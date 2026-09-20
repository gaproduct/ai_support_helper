"""Prototype of CORRECT ticket slicing that accounts for our discoveries:

  0) exclude shadow duplicates (shadow_dupes) + internal/auto-feed rows;
  1) STITCH all daily rows of a conversation (client_id[+source]) into one
     timeline (rows are disjoint → concat + dedup by (ts,text,role), sort);
  2) L1 hard-window slicing on the stitched timeline (primary_slices);
  3) L2 entity/topic split (split_by_entity);
  4) compute_incident per sub-ticket; attribute to anchor (first client
     substantive) date so multi-day tickets are counted once, on their start day.

Public: stitched_tickets_for_day(session, day) -> list[dict].
Also runnable as a script: compares BASELINE (per-row) vs STITCHED for a day.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

from sqlalchemy import text as sqlt

from database import get_session, Dialog
import resolution_metrics as rm
import ticket_slices as ts
from shadow_dupes import shadow_dialog_ids
# Шаг 0: те же фильтры не-тикетов, что и в проде (compute_tickets.py).
from compute_tickets import (
    INTERNAL_CLIENT_PREFIXES,
    EXCLUDE_CHATNAME_RE,
    EXCLUDE_CATEGORIES,
)


def _excluded_clients_for_day(session, day: date, excl: set[int]) -> set[str]:
    """client_id, которые НЕ считаем тикетами в этот день:
      • внутренний префикс клиента (напр. 25ee7201);
      • chat_name-авто-фид (напр. «Wallet … transactions»);
      • ВСЕ категории клиента за день ∈ EXCLUDE_CATEGORIES (нет ни одной
        содержательной — «Другое/Рассылка/Дубль/Тест»).
    """
    rows = session.execute(sqlt("""
        SELECT d.id, d.client_id, d.chat_name, ar.category
        FROM dialogs d
        LEFT JOIN analysis_results ar ON ar.dialog_id = d.id
        WHERE d.dialog_date = :day
    """), {"day": day}).fetchall()

    cats_by_client: dict[str, set] = defaultdict(set)
    meta_by_client: dict[str, dict] = {}
    for did, cid, chat, cat in rows:
        if did in excl:
            continue
        cats_by_client[cid].add(cat)
        m = meta_by_client.setdefault(cid, {"chat": chat})
        if chat:
            m["chat"] = chat

    excluded: set[str] = set()
    for cid, cats in cats_by_client.items():
        if any(str(cid).startswith(p) for p in INTERNAL_CLIENT_PREFIXES):
            excluded.add(cid); continue
        chat = meta_by_client[cid].get("chat")
        if chat and EXCLUDE_CHATNAME_RE.search(chat):
            excluded.add(cid); continue
        real = {c for c in cats if c and c not in EXCLUDE_CATEGORIES}
        if not real:  # ни одной содержательной категории
            excluded.add(cid); continue
    return excluded


def load(mj):
    return json.loads(mj) if isinstance(mj, str) else mj


def _msgkey(m: dict):
    return (str(m.get("created_at") or m.get("time") or m.get("ts") or ""),
            m.get("text") or "", bool(m.get("is_client")))


def stitched_timeline(session, client_id: str, excl: set[int]) -> list[dict]:
    """All daily rows of a client merged into one deduped, time-sorted list."""
    rows = [r for r in session.query(Dialog).filter(Dialog.client_id == client_id).all()
            if r.id not in excl]
    seen = set()
    merged = []
    for r in rows:
        for m in load(r.messages_json):
            k = _msgkey(m)
            if k in seen:
                continue
            seen.add(k)
            merged.append(m)
    return merged


def stitched_tickets_for_day(session, day: date):
    """Return list of (client_id, metrics, seg) for sub-tickets whose anchor
    (first client substantive) falls on `day`, using the stitched timeline."""
    excl = shadow_dialog_ids(session)
    non_tickets = _excluded_clients_for_day(session, day, excl)
    # candidate clients = those with a row on `day` (a ticket can only anchor on
    # a day if the client has messages that day)
    day_rows = [r for r in session.query(Dialog).filter(Dialog.dialog_date == day).all()
                if r.id not in excl]
    clients = sorted({r.client_id for r in day_rows})

    out = []
    for cid in clients:
        if cid in non_tickets:
            continue
        merged = stitched_timeline(session, cid, excl)
        norm = rm.normalize(merged)
        norm.sort(key=lambda x: x.ts)
        for m, seg in ts.slice_tickets(norm, normalized=True):
            anchor = next((x for x in seg if x.is_client and x.substantive), None)
            if anchor is None:
                continue
            if anchor.ts.astimezone(rm.MSK).date() != day:
                continue
            out.append((cid, m, seg))
    return out


def _baseline_counts(session, day, excl):
    non_tickets = _excluded_clients_for_day(session, day, excl)
    rows = [r for r in session.query(Dialog).filter(Dialog.dialog_date == day).all()
            if r.id not in excl and r.client_id not in non_tickets]
    c = Counter()
    for r in rows:
        for m, seg in ts.slice_tickets(load(r.messages_json)):
            c[m.status] += 1
    return c, len(rows)


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 7, 17)
    s = get_session()
    excl = shadow_dialog_ids(s)

    base, nrows = _baseline_counts(s, day, excl)
    stitched = stitched_tickets_for_day(s, day)
    sc = Counter(m.status for _, m, _ in stitched)

    order = ["resolved", "awaiting_client", "awaiting_support", "pending_department", "open"]
    bt, st = sum(base.values()), sum(sc.values())
    print(f"{day} — строк(non-shadow)={nrows}")
    print(f"{'статус':20s} {'BASELINE(по строкам)':>22s} {'STITCHED(сшито)':>18s}")
    for k in order:
        b, s2 = base.get(k, 0), sc.get(k, 0)
        bp = f"{100*b/bt:.0f}%" if bt else "-"
        sp = f"{100*s2/st:.0f}%" if st else "-"
        print(f"{k:20s} {b:6d} ({bp:>4s}){'':7s} {s2:6d} ({sp:>4s})")
    print(f"{'ИТОГО тикетов':20s} {bt:6d}{'':14s} {st:6d}")

    print("\n--- STITCHED: оставшиеся awaiting_support ---")
    for cid, m, seg in stitched:
        if m.status != "awaiting_support":
            continue
        a = next((x for x in seg if x.is_client and x.substantive), None)
        t = a.ts.astimezone(rm.MSK)
        print(f"  [{cid}] {t:%m-%d %H:%M} :: {(a.text or '').replace(chr(10),' ')[:90]!r}")


if __name__ == "__main__":
    main()
