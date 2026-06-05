"""
Flomni message-history fetcher — runs once every 24 hours.

For every IncomingMessage where done=False:
  1. Calls GET https://api.flomni.com/message/history/
     with userHash=<client_id>, limit=20, fromDate=<first_message_at>
  2. Marks the IncomingMessage as done=True.
  3. For each message in the history, creates or updates the Dialog record
     in the "История диалогов" table (analogous to Make scenario step flow).
"""

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from core.config import settings
from core.database import Dialog, IncomingMessage, get_session


log = logging.getLogger(__name__)

SOURCE = "flomni"


# ── Flomni API ───────────────────────────────────────────────────────────────

def _fetch_history(client_id: str, from_date: str = "") -> list[dict]:
    """Call Flomni /message/history/ and return the history list."""
    url = f"{settings.flomni_api_base_url}/message/history/"
    params: dict = {"userHash": client_id, "limit": 20}
    if from_date:
        params["fromDate"] = from_date
    headers = {"X-Flomni-API": settings.flomni_api_key}

    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except httpx.HTTPStatusError as exc:
        log.error("Flomni API error for client %s: %s", client_id, exc)
        return []
    except Exception as exc:
        log.error("Unexpected error fetching Flomni history for %s: %s", client_id, exc)
        return []


# ── Dialog upsert ─────────────────────────────────────────────────────────────

def _parse_message(msg: dict) -> dict:
    """Convert a raw Flomni message object into a unified dict for JSON storage.

    Unified item schema (общий для Flomni / ChatApp / будущих источников):
      text, direction, time, time_unix, type, author, author_id, message_id
    Доп. поля (phone/email/internal_id) — заполняются если есть, иначе пустые.
    """
    originator = msg.get("originator") if isinstance(msg.get("originator"), dict) else {}
    iso_time = msg.get("time") or msg.get("createdAt") or ""
    return {
        "text":       msg.get("msg", {}).get("text") or msg.get("text") or msg.get("content") or "",
        "direction":  msg.get("type", ""),         # "inbound" | "outbound"
        "time":       iso_time,
        "time_unix":  None,                         # у Flomni времени в unix нет
        "type":       "text",                       # Flomni не размечает медиа отдельно
        "author":     originator.get("name") or "",
        "author_id":  originator.get("id") or "",
        "message_id": msg.get("mid") or msg.get("id") or "",
    }


def _upsert_dialog(db: Session, client_id: str, messages: list[dict]) -> None:
    """
    Create a new Dialog or append messages to an existing open one.
    Messages are stored as a JSON array for structured querying and AI analysis.
    """
    if not messages:
        return

    dialog = (
        db.query(Dialog)
        .filter(Dialog.client_id == client_id, Dialog.source == SOURCE, Dialog.processed.is_(False))
        .first()
    )

    new_msgs = [_parse_message(m) for m in messages]

    if dialog is None:
        started_at = messages[0].get("time") or messages[0].get("createdAt") or ""
        payload = json.dumps(new_msgs, ensure_ascii=False)
        dialog = Dialog(
            client_id=client_id,
            source=SOURCE,
            messages_text=payload,
            messages_json=payload,  # обе колонки держим в одинаковом JSON-формате
            started_at=started_at,
            processed=False,
        )
        db.add(dialog)
        log.info("Created new Flomni dialog for client %s (%d messages).", client_id, len(new_msgs))
    else:
        existing_msgs: list = json.loads(dialog.messages_json or dialog.messages_text or "[]")
        existing_msgs.extend(new_msgs)
        payload = json.dumps(existing_msgs, ensure_ascii=False)
        dialog.messages_text = payload
        dialog.messages_json = payload
        dialog.updated_at = datetime.now(timezone.utc)
        log.info("Appended %d messages to Flomni dialog for client %s.", len(new_msgs), client_id)

    db.commit()


# ── Main job ─────────────────────────────────────────────────────────────────

def run() -> None:
    """Fetch message history for all pending Flomni clients."""
    log.info("Starting Flomni history fetch job.")

    with get_session() as db:
        pending: list[IncomingMessage] = (
            db.query(IncomingMessage)
            .filter(
                IncomingMessage.done.is_(False),
                IncomingMessage.source == SOURCE,
            )
            .all()
        )

    log.info("Found %d pending Flomni clients.", len(pending))

    for record in pending:
        log.info("Processing client %s (%s).", record.client_id, record.name)
        messages = _fetch_history(record.client_id, record.first_message_at or "")

        with get_session() as db:
            # Mark as done first (same as Make step 25 updateRow)
            db_record = db.get(IncomingMessage, record.id)
            if db_record:
                db_record.done = True
                db_record.last_message_at = datetime.now(timezone.utc).isoformat()
                db.commit()

            _upsert_dialog(db, record.client_id, messages)

    log.info("Flomni history fetch job complete.")


if __name__ == "__main__":
    run()
