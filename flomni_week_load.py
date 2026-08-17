"""Idempotent backfill of a week's Flomni dialogs from a core.session export.

Reads /tmp/flomni_sessions.json (fields: user_id, start ISO). For every unique
userHash it pulls history via /message/history/ (the only key the API accepts —
sessionId returns 400) bounded from that user's earliest session date, keeps only
messages inside the target week, then stores them as per-DAY Dialog rows (matching
the native pipeline). Dedup is done at the CLIENT level against ALL existing Flomni
rows so re-running (e.g. over the already-loaded Monday, or over multi-day rows)
never duplicates a message.

Dedup key: message_id when present, else (time, direction, text) — Flomni history
returns empty mids, so the fingerprint path is the norm.
"""
import json
import os
from collections import defaultdict
from datetime import date

from database import get_session, Dialog
import flomni_history as fh

SESSIONS = os.environ.get("FLOMNI_SESSIONS", "/tmp/flomni_sessions.json")
WEEK_LO = os.environ.get("FLOMNI_WEEK_LO", "2026-07-30")
WEEK_HI = os.environ.get("FLOMNI_WEEK_HI", "2026-08-01")  # exclusive


def dedup_key(m: dict):
    mid = m.get("message_id")
    if mid:
        return ("id", mid)
    return ("fp", m.get("time"), m.get("direction"), m.get("text"))


def main():
    sessions = json.load(open(SESSIONS))
    by_user = defaultdict(list)
    for s in sessions:
        if s.get("user_id"):
            by_user[s["user_id"]].append(s)

    db = get_session()
    created = updated = 0
    new_msgs_total = 0
    users_zero = []
    per_day = defaultdict(int)

    for user, sess in by_user.items():
        starts = [x["start"] for x in sess if x.get("start")]
        from_date = min(starts)[:10] + "T00:00:00.000Z" if starts else ""
        try:
            raw = fh._fetch_history(user, from_date)
        except Exception as e:
            print(f"  ! fetch error {user}: {e}")
            continue
        if not raw:
            users_zero.append(user)
            continue

        parsed = [fh._parse_message(m) for m in raw]
        week = [m for m in parsed
                if m.get("time") and WEEK_LO <= m["time"][:10] < WEEK_HI]
        if not week:
            continue

        # Dedup against ALL existing Flomni rows for this client (any date/row).
        existing_rows = (db.query(Dialog)
                         .filter(Dialog.client_id == user, Dialog.source == fh.SOURCE)
                         .all())
        seen = set()
        by_day_row = {}
        for r in existing_rows:
            for m in json.loads(r.messages_json or r.messages_text or "[]"):
                seen.add(dedup_key(m))
            if r.dialog_date is not None:
                by_day_row.setdefault(r.dialog_date.isoformat(), r)

        # Keep only truly-new messages, dedup within the fetch itself too.
        fresh = []
        local = set()
        for m in week:
            k = dedup_key(m)
            if k in seen or k in local:
                continue
            local.add(k)
            fresh.append(m)
        if not fresh:
            continue

        by_day = defaultdict(list)
        for m in fresh:
            by_day[m["time"][:10]].append(m)

        for day, msgs in by_day.items():
            msgs.sort(key=lambda x: x.get("time") or "")
            target = by_day_row.get(day)
            if target is not None:
                cur = json.loads(target.messages_json or target.messages_text or "[]")
                cur.extend(msgs)
                cur.sort(key=lambda x: x.get("time") or "")
                payload = json.dumps(cur, ensure_ascii=False)
                target.messages_text = payload
                target.messages_json = payload
                updated += 1
            else:
                payload = json.dumps(msgs, ensure_ascii=False)
                db.add(Dialog(
                    client_id=user, source=fh.SOURCE,
                    messages_text=payload, messages_json=payload,
                    started_at=msgs[0]["time"], dialog_date=date.fromisoformat(day),
                    processed=False,
                ))
                created += 1
            new_msgs_total += len(msgs)
            per_day[day] += len(msgs)
        db.commit()

    print(f"users in file: {len(by_user)}")
    print(f"created day-rows: {created}")
    print(f"updated day-rows: {updated}")
    print(f"new messages stored: {new_msgs_total}")
    print(f"users returning 0 history: {len(users_zero)}")
    print("new messages per day:", dict(sorted(per_day.items())))


if __name__ == "__main__":
    main()
