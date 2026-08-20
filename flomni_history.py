"""
Flomni message-history fetcher — runs once every 24 hours.

For every IncomingMessage where done=False:
  1. Calls GET https://api.flomni.com/message/history/
     with userHash=<client_id>, limit=500, fromDate=<first_message_at>
     Paginates via fromDate until fewer than 500 messages are returned.
  2. Marks the IncomingMessage as done=True.
  3. For each message in the history, creates or updates the Dialog record
     in the "История диалогов" table (analogous to Make scenario step flow).

Gap detection:
  detect_gap_and_backfill() is called by the scheduler on startup.
  If pending IncomingMessage records are older than 25 hours (indicating
  the scheduler was down), it runs a full history fetch immediately.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from config import settings
from database import Dialog, IncomingMessage, get_session
from extract_dialog_emails import (
    _extract_from_json,
    _extract_from_text,
    pick_executor_email,
)


log = logging.getLogger(__name__)

SOURCE = "flomni"


# ── Flomni API ───────────────────────────────────────────────────────────────

_PAGE_LIMIT = 500


def _fetch_history(client_id: str, from_date: str = "") -> list[dict]:
    """Call Flomni /message/history/ and return ALL messages via fromDate pagination.

    Fetches pages of up to _PAGE_LIMIT messages, advancing fromDate to the
    timestamp of the last message on each page until the page is incomplete.
    Deduplicates by message id to guard against timestamp-boundary overlaps.
    """
    url = f"{settings.flomni_api_base_url}/message/history/"
    headers = {"X-Flomni-API": settings.flomni_api_key}

    all_messages: list[dict] = []
    seen_ids: set[str] = set()
    current_from_date = from_date

    while True:
        params: dict = {"userHash": client_id, "limit": _PAGE_LIMIT}
        if current_from_date:
            params["fromDate"] = current_from_date

        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            page: list[dict] = resp.json().get("history", [])
        except httpx.HTTPStatusError as exc:
            log.error("Flomni API error for client %s: %s", client_id, exc)
            break
        except Exception as exc:
            log.error("Unexpected error fetching Flomni history for %s: %s", client_id, exc)
            break

        if not page:
            break

        new_count = 0
        for msg in page:
            mid = msg.get("mid") or msg.get("id") or ""
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            all_messages.append(msg)
            new_count += 1

        log.debug(
            "Flomni page for %s: got %d, new %d, total %d (fromDate=%s)",
            client_id, len(page), new_count, len(all_messages), current_from_date,
        )

        if len(page) < _PAGE_LIMIT:
            break  # last page — no more data

        # Advance fromDate to the last message's timestamp for next page.
        last_time = page[-1].get("time") or page[-1].get("createdAt") or ""
        if not last_time or last_time == current_from_date:
            log.warning(
                "Flomni pagination stalled for client %s at fromDate=%s — stopping.",
                client_id, last_time,
            )
            break
        current_from_date = last_time

    return all_messages


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


def _executor_email_from_payload(payload: str) -> str | None:
    """Extract the most likely executor email from a messages JSON payload.

    Reuses extract_dialog_emails helpers so online insert and the offline job
    share identical extraction/blacklist logic.
    """
    candidates = _extract_from_text(payload) + _extract_from_json(payload)
    return pick_executor_email(candidates)


def _upsert_dialog(
    db: Session,
    client_id: str,
    messages: list[dict],
    chat_name: str | None = None,
    client_email: str | None = None,
) -> None:
    """
    Create a new Dialog or append messages to an existing open one.
    Messages are stored as a JSON array for structured querying and AI analysis.

    On creation, populate dialog_date, chat_name (Flomni contact/group name from
    IncomingMessage.name) and executor_email so company attribution works without
    waiting for the offline backfill job.

    `client_email` — адрес из metaData вебхука. Используется запасным вариантом,
    когда в тексте переписки email не встретился: у обращений из виджета и ЛК
    это единственный источник для определения компании.
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
        from datetime import date
        try:
            dialog_date = date.fromisoformat(started_at[:10]) if started_at else None
        except ValueError:
            dialog_date = None
        payload = json.dumps(new_msgs, ensure_ascii=False)
        dialog = Dialog(
            client_id=client_id,
            source=SOURCE,
            messages_text=payload,
            messages_json=payload,  # обе колонки держим в одинаковом JSON-формате
            started_at=started_at,
            dialog_date=dialog_date,
            chat_name=(chat_name or None),
            executor_email=_executor_email_from_payload(payload) or client_email,
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
        # Backfill attribution fields if they were never set on this open dialog.
        if not dialog.chat_name and chat_name:
            dialog.chat_name = chat_name
        if not dialog.executor_email:
            dialog.executor_email = _executor_email_from_payload(payload) or client_email
        log.info("Appended %d messages to Flomni dialog for client %s.", len(new_msgs), client_id)

    db.commit()


# ── Gap detection ─────────────────────────────────────────────────────────────

_GAP_THRESHOLD_HOURS = 25  # if pending records are older than this → scheduler was down


def detect_gap_and_backfill() -> None:
    """Call this once on scheduler startup to recover from downtime.

    If there are IncomingMessage records with done=False that are older than
    _GAP_THRESHOLD_HOURS, the scheduled job was missed and we run immediately.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_GAP_THRESHOLD_HOURS)
    with get_session() as db:
        old_count = (
            db.query(IncomingMessage)
            .filter(
                IncomingMessage.done.is_(False),
                IncomingMessage.source == SOURCE,
                IncomingMessage.created_at < cutoff,
            )
            .count()
        )

    if old_count:
        log.warning(
            "Gap detected: %d Flomni pending record(s) older than %dh. "
            "Running backfill immediately.",
            old_count, _GAP_THRESHOLD_HOURS,
        )
        run()
    else:
        log.info("Gap check: no stale Flomni pending records found.")


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

            _upsert_dialog(
                db,
                record.client_id,
                messages,
                chat_name=record.name,
                client_email=record.client_email,
            )

    log.info("Flomni history fetch job complete.")


if __name__ == "__main__":
    run()
