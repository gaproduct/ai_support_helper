"""
ChatApp webhook handler.

ChatApp шлёт массив envelope'ов:
  [
    {
      "data": [ {message_item}, ... ],
      "meta": {"type": "message", "licenseId": 55868, "messengerType": "telegramBot"}
    },
    ...
  ]

Каждый message_item:
  - side: "in" (входящее от клиента) | "out" (исходящее — игнорируем)
  - type: "text" → message.text; "image"/"video"/"document"/"audio"/"voice"/"file"
          → message.caption (если пусто — плейсхолдер с именем файла)
  - chat.id: идентификатор чата → client_id
  - chat.name / fromUser.name: имя для лейбла
  - time: unix seconds

Логика 1:1 как у Flomni:
  - upsert IncomingMessage с (source='chatapp', license_id, messenger_type, client_id)
  - постит в Slack в тот же канал с тэгом 🔵 *Remozo*
  - триггерит KB-автоответ (если не приветствие)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

import auto_response
from ai_response import post_ai_response
from database import IncomingMessage, get_session
from slack_client import operator_card_button, post_slack

log = logging.getLogger(__name__)

# Тэг канала-отправителя в Slack-постах.
SLACK_TAG = "🔵 *Remozo*"

SOURCE = "chatapp"

# Ленты кошелька («Wallet XhPDT transactions») — бот-уведомления о транзакциях,
# а не обращения. Не заводим IncomingMessage и не дёргаем операторов в Slack.
# Тот же паттерн, что EXCLUDE_CHATNAME_RE в compute_tickets.
FEED_CHATNAME_RE = re.compile(r"wallet\s+\S+\s+transactions", re.I)


def _unix_to_iso(ts: int | float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _parse_message(item: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any] | None:
    """Достаём поля из одного message_item. Возвращаем None если не подходит."""
    if item.get("side") != "in":
        return None
    itype = item.get("type")
    msg = item.get("message") or {}
    if itype == "text":
        text = (msg.get("text") or "").strip()
    elif itype in {"image", "video", "document", "audio", "voice", "file"}:
        # Медиа с подписью — клиент часто шлёт скриншот + текст обращения в caption.
        text = (msg.get("caption") or "").strip()
        if not text:
            # Медиа без подписи — ставим плейсхолдер, чтобы тред в Slack всё равно завёлся.
            file_info = msg.get("file") or {}
            fname = file_info.get("name") or itype
            text = f"[{itype}] {fname}"
    else:
        return None
    if not text:
        return None
    chat = item.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return None
    from_user = item.get("fromUser") or {}
    name = (
        chat.get("name")
        or from_user.get("name")
        or chat.get("username")
        or from_user.get("username")
        or ""
    )
    if FEED_CHATNAME_RE.search(name):
        return None
    return {
        "client_id": chat_id,
        "name": name,
        "message_text": text,
        "message_time": _unix_to_iso(item.get("time")),
        "license_id": str(meta.get("licenseId") or ""),
        "messenger_type": str(meta.get("messengerType") or ""),
    }


def _build_root_blocks(client_label: str, message_text: str, incoming_id: int) -> list[dict]:
    """Block Kit для корневого сообщения треда — формат как у Flomni, тэг другой."""
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


def _create_new_incoming_message(
    db: Session,
    parsed: dict[str, Any],
) -> tuple[str, str, bool, str | None]:
    """Создаёт IncomingMessage + новый Slack-тред."""
    client_id = parsed["client_id"]
    name = parsed["name"]
    message_text = parsed["message_text"]

    should_trigger = not auto_response._is_greeting(message_text)
    client_label = name if name else client_id

    record = IncomingMessage(
        client_id=client_id,
        name=name,
        first_message_text=message_text,
        first_message_at=parsed["message_time"],
        last_message_at=parsed["message_time"],
        done=False,
        auto_response_sent=should_trigger,
        slack_thread_ts=None,
        source=SOURCE,
        license_id=parsed["license_id"] or None,
        messenger_type=parsed["messenger_type"] or None,
    )
    db.add(record)
    db.flush()  # получаем record.id для Block Kit-кнопок

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
        "New ChatApp IncomingMessage: client=%s ts=%s text=%r",
        client_id, thread_ts, message_text[:80],
    )
    return client_id, message_text, should_trigger, thread_ts


def _upsert_incoming_message(
    db: Session, parsed: dict[str, Any]
) -> tuple[str, str, bool, str | None]:
    """
    Накопление inbound-сообщений по chat_id. Каждое сообщение — пост в треде.
    Returns (client_id, message_text, should_trigger, slack_thread_ts).
    """
    client_id = parsed["client_id"]

    existing: IncomingMessage | None = (
        db.query(IncomingMessage)
        .filter(
            IncomingMessage.source == SOURCE,
            IncomingMessage.license_id == (parsed["license_id"] or None),
            IncomingMessage.messenger_type == (parsed["messenger_type"] or None),
            IncomingMessage.client_id == client_id,
            IncomingMessage.done.is_(False),
        )
        .first()
    )

    if existing:
        message_text = parsed["message_text"]
        accumulated = ((existing.first_message_text or "") + " " + message_text).strip()
        existing.first_message_text = accumulated
        existing.last_message_at = parsed["message_time"]

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
            "ChatApp append: client=%s accumulated=%r",
            client_id, accumulated[:120],
        )
        return client_id, message_text, should_trigger, existing.slack_thread_ts

    return _create_new_incoming_message(db, parsed)


def handle_chatapp_payload(payload: Any) -> dict[str, str]:
    """Унифицированный обработчик ChatApp-пейлоада. Зовётся диспатчером."""
    envelopes: list[dict] = payload if isinstance(payload, list) else [payload]
    triggers: list[tuple[str, str, str | None]] = []

    with get_session() as db:
        for env in envelopes:
            meta = env.get("meta") or {}
            if meta.get("type") and meta.get("type") != "message":
                # не сообщение (например, статус-эвент) — пропускаем
                continue
            items = env.get("data") or []
            for item in items:
                parsed = _parse_message(item, meta)
                if parsed is None:
                    continue
                client_id, message_text, should_trigger, thread_ts = _upsert_incoming_message(
                    db, parsed
                )
                if should_trigger and client_id:
                    triggers.append((client_id, message_text, thread_ts))

    for client_id, message_text, thread_ts in triggers:
        post_ai_response(message_text, thread_ts, client_id=client_id)

    return {"status": "ok"}
