"""Duplicate audit across all July dialogs (both sources).

For every client, gather all messages whose timestamp falls in July and count
duplicates by dedup key = message_id when present, else (time, direction, text).
Reports: totals per source, intra-row vs cross-row, per-day, and worst clients.
"""
import json
from collections import defaultdict, Counter
from database import get_session, Dialog

LO, HI = "2026-07-01", "2026-08-01"


def key(m):
    mid = m.get("message_id")
    if mid:
        return ("id", mid)
    return ("fp", m.get("time"), m.get("direction"), m.get("text"))


def msg_time(m):
    return (m.get("time") or "")[:10]


def in_july(m):
    t = m.get("time") or ""
    return LO <= t[:10] < HI


def main():
    s = get_session()
    for src in ("flomni", "chatapp"):
        rows = s.query(Dialog).filter(Dialog.source == src).all()
        cli_rows = defaultdict(list)
        for r in rows:
            cli_rows[r.client_id].append(r)

        total_msgs = 0
        intra = 0          # duplicate copies inside the SAME row
        cross = 0          # duplicate copies across different rows of a client
        per_day_dup = Counter()
        worst = Counter()
        rows_with_intra = 0
        clients_affected = set()

        for cid, rlist in cli_rows.items():
            seen_client = set()
            client_dup = 0
            for r in rlist:
                seen_row = set()
                row_has_intra = False
                for m in json.loads(r.messages_json or r.messages_text or "[]"):
                    if not in_july(m):
                        continue
                    total_msgs += 1
                    k = key(m)
                    if k in seen_row:
                        intra += 1
                        per_day_dup[msg_time(m)] += 1
                        client_dup += 1
                        row_has_intra = True
                    elif k in seen_client:
                        cross += 1
                        per_day_dup[msg_time(m)] += 1
                        client_dup += 1
                    seen_row.add(k)
                    seen_client.add(k)
                if row_has_intra:
                    rows_with_intra += 1
            if client_dup:
                worst[cid] = client_dup
                clients_affected.add(cid)

        print(f"===== {src} =====")
        print(f"July messages (all rows): {total_msgs}")
        print(f"duplicate copies TOTAL: {intra + cross}  (intra-row {intra}, cross-row {cross})")
        print(f"unique messages: {total_msgs - intra - cross}")
        print(f"rows with intra-row dupes: {rows_with_intra}")
        print(f"clients affected: {len(clients_affected)}")
        print(f"dupes per day: {dict(sorted(per_day_dup.items()))}")
        print("worst 10 clients:")
        for cid, n in worst.most_common(10):
            print(f"   {cid[:16]}  {n}")
        print()


if __name__ == "__main__":
    main()
