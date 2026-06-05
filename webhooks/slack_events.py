"""
Slack Events API endpoint: POST /slack/events.

Что обрабатываем:
  1) url_verification          — отдаём challenge при первичной настройке App
  2) event_callback app_mention — оператор тегнул бота в треде drafts-канала
                                  → генерим AI-черновик и постим в этот же тред
                                    с кнопкой «Отправить сообщение»
"""

import hashlib
import hmac
import json
import logging
import re
import time

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from ai.draft import generate_draft
from ai.client_name import extract_client_name
from core.config import settings
from core.database import IncomingMessage, ScenarioDraft, get_session
from slack_integration.client import get_thread_replies, post_slack, update_slack
from slack_integration.drafts import build_draft_blocks

log = logging.getLogger(__name__)

router = APIRouter()

# Slack рекомендует отбрасывать события старше 5 минут (replay-attack защита).
_MAX_EVENT_AGE_SEC = 60 * 5

# Убираем все <@UXXX> упоминания из текста — оставляем только полезную часть.
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def _verify_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Проверка подписи Slack по signing secret (HMAC-SHA256)."""
    if not settings.slack_signing_secret:
        # Signing secret не настроен — пропускаем проверку (dev/staging).
        log.warning("slack_signing_secret not set, skipping signature check.")
        return True
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > _MAX_EVENT_AGE_SEC:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}".encode()
    digest = hmac.new(
        settings.slack_signing_secret.encode(),
        basestring,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature or "")


def _extract_operator_text(event: dict) -> str:
    raw = (event.get("text") or "").strip()
    cleaned = _MENTION_RE.sub("", raw).strip()
    # схлопываем пробелы
    return re.sub(r"\s+", " ", cleaned)


def _handle_app_mention(event: dict) -> None:
    """Reply with an AI draft when bot is mentioned inside a tracked drafts thread."""
    channel = event.get("channel") or ""
    thread_ts = event.get("thread_ts") or event.get("ts") or ""
    if not channel or not thread_ts:
        return

    with get_session() as db:
        draft: ScenarioDraft | None = (
            db.query(ScenarioDraft)
            .filter(
                ScenarioDraft.draft_channel == channel,
                ScenarioDraft.draft_thread_ts == thread_ts,
            )
            .first()
        )
        if draft is None:
            log.info(
                "app_mention in untracked thread: channel=%s thread_ts=%s — ignoring",
                channel, thread_ts,
            )
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
        draft_id = draft.id
        # Координаты активной копии черновика — если уже есть, апдейтим её,
        # вместо того чтобы плодить новые сообщения.
        active_draft_ts = draft.active_draft_msg_ts
        mirror_channel = draft.mirror_channel
        mirror_msg_ts = draft.mirror_msg_ts
        original_thread_ts = draft.original_thread_ts
        original_channel = draft.original_channel

    # Собираем историю треда: пропускаем «корневое» уведомление сценария,
    # любые сообщения бота (включая ранее сгенерированные черновики — они бы
    # путали модель) и пустые/служебные.
    replies = get_thread_replies(channel, thread_ts, limit=50)
    history: list[str] = []
    for msg in replies:
        if msg.get("ts") == thread_ts:
            continue
        if msg.get("bot_id") or msg.get("subtype") == "bot_message":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        history.append(text)

    operator_instruction = _extract_operator_text(event)

    draft_text = generate_draft(
        scenario_name=scenario_name,
        scenario_notification=scenario_notification,
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

    new_blocks = build_draft_blocks(draft_text, draft_id)
    # Если активный черновик уже есть — апдейтим обе копии in-place,
    # иначе постим новые и сохраняем координаты.
    if active_draft_ts:
        update_slack(
            channel=channel,
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
        return

    new_active_ts = post_slack(
        text="Черновик ответа клиенту готов.",
        channel=channel,
        thread_ts=thread_ts,
        blocks=new_blocks,
    )
    new_mirror_ts = None
    if original_thread_ts:
        new_mirror_ts = post_slack(
            text="Черновик ответа клиенту готов.",
            channel=original_channel or settings.slack_channel_id,
            thread_ts=original_thread_ts,
            blocks=new_blocks,
        )
    with get_session() as db:
        d = db.query(ScenarioDraft).filter(ScenarioDraft.id == draft_id).first()
        if d:
            d.active_draft_msg_ts = new_active_ts
            d.mirror_channel = (original_channel or settings.slack_channel_id) if new_mirror_ts else None
            d.mirror_msg_ts = new_mirror_ts
            db.commit()


@router.post("/slack/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
    x_slack_retry_num: str | None = Header(default=None),
    x_slack_retry_reason: str | None = Header(default=None),
):
    body = await request.body()
    if not _verify_signature(body, x_slack_request_timestamp or "", x_slack_signature or ""):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    # 1) URL verification (one-time при настройке Event Subscriptions)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    # 2) Event callback
    if payload.get("type") == "event_callback":
        # Игнорируем ретраи Slack (генерация занимает > 3s — Slack считает
        # это таймаутом и пересылает событие; без этой защиты получаем
        # дублирующиеся черновики).
        if x_slack_retry_num and x_slack_retry_num.isdigit() and int(x_slack_retry_num) > 0:
            log.info(
                "Skipping Slack retry: num=%s reason=%s",
                x_slack_retry_num, x_slack_retry_reason,
            )
            return {"ok": True}

        event = payload.get("event") or {}
        # Игнорируем сообщения, отправленные ботом самим (bot_id / subtype).
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return {"ok": True}

        if event.get("type") == "app_mention":
            # Возвращаем 200 OK немедленно, тяжёлую работу делаем в фоне —
            # генерация черновика + пост в Slack могут занять > 3 секунд.
            background_tasks.add_task(_safe_handle_app_mention, event)

    return {"ok": True}


def _safe_handle_app_mention(event: dict) -> None:
    try:
        _handle_app_mention(event)
    except Exception as exc:
        log.exception("app_mention handler error: %s", exc)
