"""
Flomni webhook receiver.

Flomni sends a POST request with an array payload:
  [
    {
      "receiver": "<client_id>",
      "profile": { "name": "..." },
      "content": [
        {
          "msg": { "text": "<message text>", ... },
          "time": "2026-04-29T21:13:11.888Z",
          "type": "inbound"
        }
      ]
    }
  ]
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

import auto_response
from ai_response import post_ai_response
from config import settings
from database import IncomingMessage, get_session
from slack_client import operator_card_button, post_slack

log = logging.getLogger(__name__)

router = APIRouter()

# Тэг канала-отправителя в Slack-постах.
SLACK_TAG = "🟢 *Madetask*"


def _verify_signature(body: bytes, signature: str | None) -> None:
    """Verify HMAC-SHA256 signature if a webhook secret is configured."""
    secret = settings.flomni_webhook_secret
    if not secret:
        return
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# ── Парные получатели ────────────────────────────────────────────────────────
#
# Один TG-чат подключён к Flomni двумя каналами. Приходят два вебхука с разными
# `receiver` и одинаковым текстом, разница около 150 мс. Раньше близнецов искали
# по названию чата, но названия у каналов разные: один отдаёт имя группы
# («Дзен&MadeTask»), второй имя человека («Dmitriy»). Совпадает только текст.
#
# Близнеца не выбрасываем: непонятно, чей канал записал ответы поддержки, а
# в истории второго встречаются сообщения клиента, которых нет у первого.
# Вместо этого делим один тред: запись заводим, в Slack и в автоответчик не идём.
# Кто из пары канонический, разбирает flomni_twins.py по ночным данным.

_TWIN_WINDOW_SECONDS = 5.0

# Короткие реплики («Добрый день») совпадают у посторонних клиентов.
_TWIN_MIN_TEXT_LEN = 20


def _parse_iso(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _open_record(db: Session, client_id: str) -> IncomingMessage | None:
    return (
        db.query(IncomingMessage)
        .filter(
            IncomingMessage.source == "flomni",
            IncomingMessage.client_id == client_id,
            IncomingMessage.done.is_(False),
        )
        .first()
    )


def _text_twin(
    db: Session, client_id: str, message_text: str, message_time: str
) -> IncomingMessage | None:
    """Открытая запись другого получателя с тем же текстом в те же секунды."""
    body = (message_text or "").strip()
    at = _parse_iso(message_time)
    if at is None or len(body) < _TWIN_MIN_TEXT_LEN:
        return None

    candidates = (
        db.query(IncomingMessage)
        .filter(
            IncomingMessage.source == "flomni",
            IncomingMessage.done.is_(False),
            IncomingMessage.client_id != client_id,
        )
        .all()
    )
    for candidate in candidates:
        other = _parse_iso(candidate.last_message_at)
        if other is None or abs(at - other) > _TWIN_WINDOW_SECONDS:
            continue
        if (candidate.first_message_text or "").strip().endswith(body):
            return candidate
    return None


def _resolve_twin(
    db: Session, client_id: str, message_text: str, message_time: str
) -> tuple[str | None, str | None]:
    """Определяет, второй ли это канал уже известного чата.

    Возвращает (client_id близнеца, ts общего треда). Оба None, если чат свой.

    Сначала смотрим реестр: там канонический получатель выбран по ночным данным,
    и порядок доставки вебхуков уже не важен. Если пары в реестре нет, ищем
    открытую запись с тем же текстом в те же секунды. Такой поиск зависит от
    того, кто пришёл первым, зато ловит ещё не разобранные пары.
    """
    row = db.execute(
        text("SELECT canonical_client_id FROM flomni_twin_receivers "
             "WHERE shadow_client_id = :s"),
        {"s": client_id},
    ).first()
    if row:
        canonical = _open_record(db, row[0])
        return row[0], canonical.slack_thread_ts if canonical else None

    twin = _text_twin(db, client_id, message_text, message_time)
    if twin is None:
        return None, None
    # Близнец сам мог оказаться близнецом: цепляемся к корню, чтобы не строить
    # цепочку A -> B -> C и не потерять исходный тред.
    return twin.twin_of or twin.client_id, twin.slack_thread_ts


def _build_root_blocks(
    client_label: str, message_text: str, incoming_id: int
) -> list[dict]:
    """Block Kit для корневого сообщения треда: текст + кнопки действий."""
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📜 История диалога"},
            "action_id": "show_history",
            "value": str(incoming_id),
        },
    ]
    card = operator_card_button(incoming_id)
    if card:
        buttons.append(card)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{SLACK_TAG} | 👤 *Новое обращение* | {client_label}\n\n> {message_text}",
            },
        },
        {"type": "actions", "elements": buttons},
    ]


# Контакты клиента Flomni кладёт в metaData, а не в profile: у виджета на сайте
# и у ЛК заказчика profile приходит пустым. Названия полей задаются в настройках
# канала, поэтому сверяем ключи без учёта регистра и лишних пробелов.
_METADATA_NAME_KEYS = frozenset({"имя клиента", "имя", "name", "client name"})
_METADATA_EMAIL_KEYS = frozenset({"email", "e-mail", "почта", "почта клиента"})

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def _parse_metadata(event: dict[str, Any]) -> tuple[str, str]:
    """Достаём (имя, email) из metaData. Пустые строки, если их там нет."""
    meta = event.get("metaData")
    if not isinstance(meta, dict):
        return "", ""

    name = ""
    email = ""
    for raw_key, raw_value in meta.items():
        if not isinstance(raw_value, str):
            continue
        key = str(raw_key).strip().lower()
        value = raw_value.strip()
        if not value:
            continue
        if not name and key in _METADATA_NAME_KEYS:
            name = value
        if not email and key in _METADATA_EMAIL_KEYS and _EMAIL_RE.fullmatch(value):
            email = value.lower()
    return name, email


def _parse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Раскладываем событие Flomni на поля, которые пишем в IncomingMessage."""
    content = event.get("content", [])
    first = content[0] if content else {}
    profile_name = (event.get("profile") or {}).get("name", "") or ""
    meta_name, meta_email = _parse_metadata(event)
    return {
        "client_id": str(event.get("receiver", "")),
        # profile.name — название группового чата («КЛИЕНТ × MT»), по нему потом
        # определяется компания. metaData даёт имя человека: годится как подпись
        # в Slack, но компанию из него не вытащить. Поэтому profile.name первый.
        "name": profile_name or meta_name,
        "client_email": meta_email,
        "message_text": first.get("msg", {}).get("text", "") or "",
        "message_time": first.get("time", "") or "",
    }


def _create_new_incoming_message(
    db: Session,
    parsed: dict[str, Any],
    twin_of: str | None = None,
    twin_thread_ts: str | None = None,
) -> tuple[str, str, bool, str | None]:
    """
    Создаёт новый IncomingMessage + новый Slack-тред для клиента.

    Если задан `twin_of`, это второй канал того же TG-чата. Запись заводим, чтобы
    забрать историю, но тред у неё общий с близнецом и в Slack мы не пишем.
    """
    client_id = parsed["client_id"]
    name = parsed["name"]
    message_text = parsed["message_text"]
    message_time = parsed["message_time"]

    accumulated = message_text
    should_trigger = twin_of is None and not auto_response._is_greeting(message_text)

    client_label = name if name else client_id

    # Создаём запись сразу — чтобы её id можно было прокинуть в кнопки Block Kit
    # (id нужен для action handler'ов «История диалога» / «Сгенерировать ответ»).
    record = IncomingMessage(
        client_id=client_id,
        name=name,
        client_email=parsed["client_email"] or None,
        first_message_text=accumulated,
        first_message_at=message_time,
        last_message_at=message_time,
        done=False,
        auto_response_sent=should_trigger,
        slack_thread_ts=None,
        twin_of=twin_of,
    )
    db.add(record)
    db.flush()  # получаем record.id, но без commit — thread_ts проставим ниже

    if twin_of is not None:
        record.slack_thread_ts = twin_thread_ts
        db.commit()
        log.info(
            "TG-twin: created silent record client=%s twin=%s name=%r",
            client_id, twin_of, name,
        )
        return "", "", False, None

    blocks = _build_root_blocks(client_label, message_text, record.id)
    thread_ts = post_slack(
        text=f"{SLACK_TAG} | 👤 *Новое обращение* | {client_label}\n\n> {message_text}",
        blocks=blocks,
    )
    record.slack_thread_ts = thread_ts

    if not should_trigger:
        post_slack(
            f"{SLACK_TAG} | 🤖 *AI-ассистент* ожидает вопроса от клиента",
            thread_ts=thread_ts,
        )

    db.commit()
    log.info(
        "New IncomingMessage: client=%s ts=%s text=%r",
        client_id, thread_ts, message_text[:80],
    )
    return client_id, message_text, should_trigger, thread_ts


def _upsert_incoming_message(
    db: Session, event: dict[str, Any]
) -> tuple[str, str, bool, str | None]:
    """
    Accumulate inbound message text into the open IncomingMessage record.
    Posts every client message to Slack (new post or thread reply).

    Если входящее сообщение по гибридной эвристике относится к новой теме,
    закрываем текущий IncomingMessage и открываем свежий тред.

    Returns (client_id, message_text_for_ai, should_trigger, slack_thread_ts).
    """
    parsed = _parse_event(event)
    client_id = parsed["client_id"]
    name = parsed["name"]
    message_text = parsed["message_text"]
    message_time = parsed["message_time"]

    if not client_id:
        log.warning("Webhook event missing 'receiver': %s", event)
        return "", "", False, None

    existing: IncomingMessage | None = (
        db.query(IncomingMessage)
        .filter(
            IncomingMessage.source == "flomni",
            IncomingMessage.client_id == client_id,
            IncomingMessage.done.is_(False),
        )
        .first()
    )

    if existing:
        # Topic-boundary detection temporarily disabled — собираем весь диалог
        # по clientID в один Slack-тред без попыток разделить на отдельные вопросы.
        accumulated = ((existing.first_message_text or "") + " " + message_text).strip()
        existing.first_message_text = accumulated
        existing.last_message_at = message_time
        # Клиент мог представиться не в первом сообщении, а позже.
        if not existing.client_email and parsed["client_email"]:
            existing.client_email = parsed["client_email"]
        if not existing.name and name:
            existing.name = name

        if existing.twin_of:
            db.commit()
            log.info(
                "TG-twin: silent append client=%s twin=%s",
                client_id, existing.twin_of,
            )
            return "", "", False, None

        post_slack(
            f"{SLACK_TAG} | 💬 *Клиент:* {message_text}",
            thread_ts=existing.slack_thread_ts,
        )

        should_trigger = not auto_response._is_greeting(message_text)
        if not should_trigger:
            post_slack(
                f"{SLACK_TAG} | 🤖 *AI-ассистент* ожидает вопроса от клиента",
                thread_ts=existing.slack_thread_ts,
            )

        db.commit()
        log.info(
            "Appended message for client=%s accumulated=%r",
            client_id, accumulated[:120],
        )
        # Pass only the new message_text to AI analysis
        return client_id, message_text, should_trigger, existing.slack_thread_ts

    # Записи ещё нет. Проверяем, не второй ли это канал уже открытого чата.
    twin_of, twin_thread_ts = _resolve_twin(db, client_id, message_text, message_time)
    return _create_new_incoming_message(db, parsed, twin_of, twin_thread_ts)


def handle_flomni_payload(payload: Any) -> dict[str, str]:
    """Унифицированный обработчик. Вызывается и /webhook/flomni, и /webhook/inbound."""
    events: list[dict] = payload if isinstance(payload, list) else [payload]
    triggers: list[tuple[str, str, str | None]] = []

    with get_session() as db:
        for event in events:
            content = event.get("content", [{}])
            msg_type = content[0].get("type", "") if content else ""
            if msg_type != "inbound":
                continue
            event_text = (content[0].get("msg", {}).get("text", "") or "") if content else ""
            if not event_text.strip():
                log.info(
                    "Skipping non-text inbound event (likely TG system event) client=%s",
                    event.get("receiver", ""),
                )
                continue
            client_id, message_text, should_trigger, thread_ts = _upsert_incoming_message(
                db, event
            )
            if should_trigger and client_id:
                triggers.append((client_id, message_text, thread_ts))

    for client_id, message_text, thread_ts in triggers:
        post_ai_response(message_text, thread_ts, client_id=client_id)

    return {"status": "ok"}


@router.post("/webhook/flomni")
async def flomni_webhook(
    request: Request,
    x_flomni_signature: str | None = Header(default=None),
) -> dict[str, str]:
    """
    Legacy URL. Сейчас принимает И Flomni, И ChatApp пейлоады — внутри диспатчится
    по форме (см. webhooks.inbound.detect_source). Сделано для backward-compat,
    поскольку ChatApp уже настроен слать сюда.
    """
    body = await request.body()

    try:
        payload: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    # Локальный импорт — избегаем циклической зависимости (inbound импортирует flomni).
    from webhooks.inbound import detect_source
    from webhooks.chatapp import handle_chatapp_payload

    source = detect_source(payload)
    if source == "chatapp":
        log.info("/webhook/flomni routed to chatapp handler")
        return handle_chatapp_payload(payload)

    # Flomni — здесь проверяем подпись (для ChatApp её нет).
    _verify_signature(body, x_flomni_signature)
    return handle_flomni_payload(payload)
