"""
Slack Interactivity endpoint: POST /slack/interactions.

Принимает payload от кликов по интерактивным элементам (кнопкам Block Kit).
Сейчас поддерживается только action_id="send_draft" — заглушка, пока шаблоны
исходящих сообщений Flomni не настроены.
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from ai_draft import generate_draft
from client_name import extract_client_name
from config import settings
from database import IncomingMessage, ScenarioDraft, get_session
from flomni_history import _fetch_history
from slack_client import get_thread_replies, post_slack, update_slack
from slack_drafts import build_draft_blocks

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_AGE_SEC = 60 * 5


def _verify_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not settings.slack_signing_secret:
        log.warning("slack_signing_secret not set, skipping signature check.")
        return True
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > _MAX_AGE_SEC:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}".encode()
    digest = hmac.new(
        settings.slack_signing_secret.encode(),
        basestring,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature or "")


def _regenerate(channel: str, thread_ts: str, draft_msg_ts: str, draft_id_str: str) -> None:
    """Перегенерация черновика: in-place chat.update обеих копий — в drafts-канале
    и в зеркале (исходный тред основного канала)."""
    try:
        draft_id = int(draft_id_str)
    except (TypeError, ValueError):
        log.warning("regenerate: invalid draft_id=%r", draft_id_str)
        return

    with get_session() as db:
        draft: ScenarioDraft | None = (
            db.query(ScenarioDraft).filter(ScenarioDraft.id == draft_id).first()
        )
        if draft is None:
            log.warning("regenerate: ScenarioDraft id=%s not found", draft_id)
            return
        incoming: IncomingMessage | None = (
            db.query(IncomingMessage)
            .filter(IncomingMessage.id == draft.incoming_message_id)
            .first()
        )
        client_message = (incoming.first_message_text if incoming else "") or ""
        client_label = extract_client_name(incoming.name) if incoming else ""
        scenario_name = draft.scenario_name
        scenario_notification = draft.notification_text or ""
        # Координаты обеих копий из БД (active_* — в drafts, mirror_* — в исходном треде).
        drafts_channel = draft.draft_channel
        active_draft_ts = draft.active_draft_msg_ts or draft_msg_ts
        mirror_channel = draft.mirror_channel
        mirror_msg_ts = draft.mirror_msg_ts
        drafts_thread_ts = draft.draft_thread_ts

    # Историю собираем именно из drafts-треда — там операторы общаются.
    # Если регенерация прилетела по клику в mirror — всё равно идём за источником.
    history_channel = drafts_channel or channel
    history_thread = drafts_thread_ts or thread_ts
    # Slack API может вернуть missing_scope / network error — не валим регенерацию,
    # просто продолжаем без истории треда.
    try:
        replies = get_thread_replies(history_channel, history_thread, limit=50)
    except Exception as exc:
        log.warning("regenerate: failed to fetch thread replies, continuing without history: %s", exc)
        replies = []
    history: list[str] = []
    operator_instruction = ""
    for msg in replies:
        if msg.get("ts") == history_thread:
            continue
        if msg.get("bot_id") or msg.get("subtype") == "bot_message":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        history.append(text)
        operator_instruction = text

    new_text = generate_draft(
        scenario_name=scenario_name,
        scenario_notification=scenario_notification,
        client_message=client_message,
        thread_history=history,
        operator_instruction=operator_instruction,
        client_name=client_label,
    )
    if not new_text:
        post_slack(
            "⚠️ Не удалось перегенерировать черновик "
            "(OpenAI недоступен или вернул пустой ответ).",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    new_blocks = build_draft_blocks(new_text, draft_id)
    if drafts_channel and active_draft_ts:
        update_slack(
            channel=drafts_channel,
            ts=active_draft_ts,
            text="Черновик ответа клиенту готов.",
            blocks=new_blocks,
        )
    if mirror_channel and mirror_msg_ts:
        update_slack(
            channel=mirror_channel,
            ts=mirror_msg_ts,
            text="Черновик ответа клиенту готов.",
            blocks=new_blocks,
        )


def _build_reply_blocks(draft_text: str, incoming_id: int) -> list[dict]:
    """Block Kit для generic-черновика (без сценария), генерится по кнопке."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "✏️ *Черновик ответа клиенту:*\n" + f"```{draft_text}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Отправить сообщение"},
                    "action_id": "send_reply",
                    "value": str(incoming_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔄 Перегенерировать"},
                    "action_id": "regenerate_reply",
                    "value": str(incoming_id),
                },
            ],
        },
    ]


def _collect_thread_context(
    channel: str, thread_ts: str
) -> tuple[list[str], str]:
    """Собирает историю не-бот сообщений треда + берёт последнее как «указание оператора»."""
    # Slack API может вернуть missing_scope / network error — генерация черновика
    # должна работать и без истории, поэтому не падаем.
    try:
        replies = get_thread_replies(channel, thread_ts, limit=50)
    except Exception as exc:
        log.warning("collect_thread_context: failed to fetch replies, continuing empty: %s", exc)
        replies = []
    history: list[str] = []
    operator_instruction = ""
    for msg in replies:
        if msg.get("ts") == thread_ts:
            continue
        if msg.get("bot_id") or msg.get("subtype") == "bot_message":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        history.append(text)
        operator_instruction = text
    return history, operator_instruction


def _generate_reply_draft(
    incoming_id_str: str, channel: str, thread_ts: str
) -> None:
    """Сгенерировать generic-черновик и запостить новым сообщением в этот тред."""
    try:
        incoming_id = int(incoming_id_str)
    except (TypeError, ValueError):
        log.warning("generate_reply: invalid incoming_id=%r", incoming_id_str)
        return

    with get_session() as db:
        rec: IncomingMessage | None = db.get(IncomingMessage, incoming_id)
        if rec is None:
            log.warning("generate_reply: IncomingMessage id=%s not found", incoming_id)
            return
        client_message = rec.first_message_text or ""
        client_label = extract_client_name(rec.name)

    history, operator_instruction = _collect_thread_context(channel, thread_ts)

    draft_text = generate_draft(
        scenario_name="",
        scenario_notification="",
        client_message=client_message,
        thread_history=history,
        operator_instruction=operator_instruction,
        client_name=client_label,
    )
    if not draft_text:
        post_slack(
            "⚠️ Не удалось сгенерировать черновик "
            "(OpenAI недоступен или вернул пустой ответ).",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    post_slack(
        text="Черновик ответа клиенту готов.",
        channel=channel,
        thread_ts=thread_ts,
        blocks=_build_reply_blocks(draft_text, incoming_id),
    )


def _regenerate_reply_draft(
    incoming_id_str: str, channel: str, thread_ts: str, msg_ts: str
) -> None:
    """Перегенерировать generic-черновик in-place (chat.update)."""
    try:
        incoming_id = int(incoming_id_str)
    except (TypeError, ValueError):
        log.warning("regenerate_reply: invalid incoming_id=%r", incoming_id_str)
        return

    with get_session() as db:
        rec: IncomingMessage | None = db.get(IncomingMessage, incoming_id)
        if rec is None:
            return
        client_message = rec.first_message_text or ""
        client_label = extract_client_name(rec.name)

    history, operator_instruction = _collect_thread_context(channel, thread_ts)

    new_text = generate_draft(
        scenario_name="",
        scenario_notification="",
        client_message=client_message,
        thread_history=history,
        operator_instruction=operator_instruction,
        client_name=client_label,
    )
    if not new_text:
        post_slack(
            "⚠️ Не удалось перегенерировать черновик.",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    update_slack(
        channel=channel,
        ts=msg_ts,
        text="Черновик ответа клиенту готов.",
        blocks=_build_reply_blocks(new_text, incoming_id),
    )


def _show_history(
    incoming_id_str: str, channel: str, thread_ts: str
) -> None:
    """Подтянуть историю диалога из Flomni API и положить её в тред."""
    try:
        incoming_id = int(incoming_id_str)
    except (TypeError, ValueError):
        log.warning("show_history: invalid incoming_id=%r", incoming_id_str)
        return

    with get_session() as db:
        rec: IncomingMessage | None = db.get(IncomingMessage, incoming_id)
        if rec is None:
            post_slack(
                "📜 История диалога недоступна (запись не найдена).",
                channel=channel,
                thread_ts=thread_ts,
            )
            return
        client_id = rec.client_id

    # Тестовые клиенты, заведённые вручную для прогонов сценариев, в Flomni
    # отсутствуют — API на них возвращает 404. Сразу отдаём понятный ответ.
    if client_id.startswith("test-"):
        post_slack(
            f"📜 История диалога недоступна: `{client_id}` — тестовый клиент, "
            "его нет в Flomni.",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    messages = _fetch_history(client_id, "")
    if not messages:
        post_slack(
            "📜 История диалога: пусто или Flomni API не вернул данные.",
            channel=channel,
            thread_ts=thread_ts,
        )
        return

    # Сортируем по времени по возрастанию: старые сверху, новые снизу — так
    # хронология диалога читается естественно.
    def _ts_key(m: dict) -> str:
        return m.get("time") or m.get("createdAt") or ""

    messages_sorted = sorted(messages, key=_ts_key)

    # Direction: outbound — сообщение от поддержки, inbound — от клиента.
    lines: list[str] = ["📜 *История диалога с клиентом:*", "```"]
    for m in messages_sorted:
        direction = m.get("type", "")
        text = (
            m.get("msg", {}).get("text")
            or m.get("text")
            or m.get("content")
            or ""
        )
        if not text:
            continue
        ts = m.get("time") or m.get("createdAt") or ""
        label = "Поддержка" if direction == "outbound" else "Клиент"
        # Обрезаем длинные сообщения, чтобы пост влез в лимит Slack (~3000 символов).
        snippet = text if len(text) <= 300 else text[:300] + "…"
        lines.append(f"[{ts}] {label}: {snippet}")
    lines.append("```")

    text_body = "\n".join(lines)
    # Section text лимит у Slack ~3000 — оставляем запас под закрывающий ```.
    if len(text_body) > 2900:
        text_body = text_body[:2900] + "\n…```"

    history_blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text_body},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✨ Сгенерировать ответ"},
                    "action_id": "generate_reply",
                    "value": str(incoming_id),
                },
            ],
        },
    ]
    post_slack(
        text="📜 История диалога с клиентом",
        channel=channel,
        thread_ts=thread_ts,
        blocks=history_blocks,
    )


@router.post("/slack/interactions")
async def slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
):
    raw_body = await request.body()
    if not _verify_signature(
        raw_body, x_slack_request_timestamp or "", x_slack_signature or ""
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Slack шлёт application/x-www-form-urlencoded с единственным ключом "payload"
    parsed = parse_qs(raw_body.decode("utf-8", errors="replace"))
    payload_list = parsed.get("payload") or []
    if not payload_list:
        raise HTTPException(status_code=400, detail="missing payload")
    try:
        data = json.loads(payload_list[0])
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    actions = data.get("actions") or []
    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id")
    channel = (data.get("channel") or {}).get("id") or ""
    message = data.get("message") or {}
    thread_ts = message.get("thread_ts") or message.get("ts") or ""
    user = (data.get("user") or {}).get("username") or "оператор"

    if action_id in ("send_draft", "send_reply"):
        # Шаблоны исходящих сообщений Flomni ещё не настроены — заглушка.
        post_slack(
            f"⏸️ <@{user}>, шаблоны исходящих сообщений Flomni ещё не подключены. "
            f"Когда они будут готовы, эта кнопка будет реально отправлять текст клиенту.",
            channel=channel,
            thread_ts=thread_ts,
        )
        log.info("%s click acknowledged (template send disabled).", action_id)

    elif action_id == "regenerate_draft":
        draft_id_str = action.get("value") or ""
        draft_msg_ts = message.get("ts") or ""
        # Запускаем в фоне — Slack ждёт ответ в течение 3 секунд.
        background_tasks.add_task(
            _regenerate, channel, thread_ts, draft_msg_ts, draft_id_str
        )

    elif action_id == "generate_reply":
        # Кнопка на корневом сообщении треда — сгенерировать generic-черновик.
        incoming_id_str = action.get("value") or ""
        background_tasks.add_task(
            _generate_reply_draft, incoming_id_str, channel, thread_ts
        )

    elif action_id == "regenerate_reply":
        incoming_id_str = action.get("value") or ""
        msg_ts = message.get("ts") or ""
        background_tasks.add_task(
            _regenerate_reply_draft, incoming_id_str, channel, thread_ts, msg_ts
        )

    elif action_id == "show_history":
        incoming_id_str = action.get("value") or ""
        background_tasks.add_task(
            _show_history, incoming_id_str, channel, thread_ts
        )

    return {"ok": True}
