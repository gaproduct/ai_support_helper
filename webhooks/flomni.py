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
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ai import auto_response
from ai.response import post_ai_response
from core.config import settings
from core.database import IncomingMessage, get_session
from slack_integration.client import post_slack

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


def _normalize_channel_name(name: str) -> str:
    """Normalize a channel/chat name for TG-duplicate detection.

    Lowercases and reduces the name to a sorted set of alnum tokens (Latin/Cyrillic/digits).
    Punctuation, emojis, parentheses, ampersands etc. are stripped. Token order
    is normalized so that "A & B (X)" and "B X & A" produce the same key.
    """
    if not name:
        return ""
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", name.lower())
    return " ".join(sorted(tokens))


def _build_root_blocks(
    client_label: str, message_text: str, incoming_id: int
) -> list[dict]:
    """Block Kit для корневого сообщения треда: текст + кнопки действий."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{SLACK_TAG} | 👤 *Новое обращение* | {client_label}\n\n> {message_text}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📜 История диалога"},
                    "action_id": "show_history",
                    "value": str(incoming_id),
                },
            ],
        },
    ]


def _parse_event(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """Returns (client_id, name, message_text, message_time)."""
    client_id = str(event.get("receiver", ""))
    name = event.get("profile", {}).get("name", "") or ""
    content = event.get("content", [])
    first = content[0] if content else {}
    message_text = first.get("msg", {}).get("text", "") or ""
    message_time = first.get("time", "") or ""
    return client_id, name, message_text, message_time


def _create_new_incoming_message(
    db: Session,
    client_id: str,
    name: str,
    message_text: str,
    message_time: str,
) -> tuple[str, str, bool, str | None]:
    """
    Создаёт новый IncomingMessage + новый Slack-тред для клиента.
    """
    accumulated = message_text
    should_trigger = not auto_response._is_greeting(message_text)

    client_label = name if name else client_id

    # Создаём запись сразу — чтобы её id можно было прокинуть в кнопки Block Kit
    # (id нужен для action handler'ов «История диалога» / «Сгенерировать ответ»).
    record = IncomingMessage(
        client_id=client_id,
        name=name,
        first_message_text=accumulated,
        first_message_at=message_time,
        last_message_at=message_time,
        done=False,
        auto_response_sent=should_trigger,
        slack_thread_ts=None,
    )
    db.add(record)
    db.flush()  # получаем record.id, но без commit — thread_ts проставим ниже

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
    client_id, name, message_text, message_time = _parse_event(event)

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

    # TG-duplicate fallback: when Flomni delivers the same TG group via two
    # different connectors (different receiver IDs but the same channel name),
    # route the second webhook to the existing record instead of creating a twin.
    if not existing:
        normalized = _normalize_channel_name(name)
        if normalized:
            recent_cutoff = datetime.utcnow() - timedelta(hours=24)
            candidates = (
                db.query(IncomingMessage)
                .filter(
                    IncomingMessage.source == "flomni",
                    IncomingMessage.done.is_(False),
                    IncomingMessage.client_id != client_id,
                    IncomingMessage.created_at >= recent_cutoff,
                )
                .all()
            )
            twin = next(
                (
                    c for c in candidates
                    if _normalize_channel_name(c.name or "") == normalized
                ),
                None,
            )
            if twin is not None:
                accumulated_tail = (twin.first_message_text or "").strip()
                new_text = (message_text or "").strip()
                if new_text and accumulated_tail.endswith(new_text):
                    log.info(
                        "Skipping TG-twin duplicate (same text) client=%s twin=%s name=%r",
                        client_id, twin.client_id, name,
                    )
                    return "", "", False, None
                log.info(
                    "Routing TG-twin message to existing record: client=%s -> twin_client=%s name=%r",
                    client_id, twin.client_id, name,
                )
                existing = twin

    if existing:
        # Topic-boundary detection temporarily disabled — собираем весь диалог
        # по clientID в один Slack-тред без попыток разделить на отдельные вопросы.
        accumulated = ((existing.first_message_text or "") + " " + message_text).strip()
        existing.first_message_text = accumulated
        existing.last_message_at = message_time

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

    return _create_new_incoming_message(
        db, client_id, name, message_text, message_time
    )


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
