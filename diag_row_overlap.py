"""Diagnostic: are per-day dialog rows disjoint (each = that day's msgs) or
cumulative snapshots (each = full history)? Check message-timestamp overlap
across the daily rows of a few multi-row clients."""
import json
from collections import defaultdict

from database import get_session, Dialog
from shadow_dupes import shadow_dialog_ids


def load(mj):
    return json.loads(mj) if isinstance(mj, str) else mj


def sig(m):
    return (str(m.get("created_at") or m.get("time") or m.get("ts") or ""),
            (m.get("text") or "")[:40])


def main():
    s = get_session()
    excl = shadow_dialog_ids(s)
    # pick clients with many rows
    samples = ["fc1e4ebf", "25ee7201", "5819035206"]
    for cid in samples:
        rows = [r for r in s.query(Dialog).filter(Dialog.client_id.like(f"%{cid}%")).all()
                if r.id not in excl]
        rows.sort(key=lambda r: str(r.dialog_date))
        print(f"\n=== {cid}: {len(rows)} rows ===")
        seen_row_sigs = []
        all_msgs = 0
        for r in rows:
            msgs = load(r.messages_json)
            sigs = set(sig(m) for m in msgs)
            all_msgs += len(msgs)
            # overlap with union of prior rows
            prior = set().union(*seen_row_sigs) if seen_row_sigs else set()
            ov = len(sigs & prior)
            print(f"  d{r.id} {r.dialog_date} msgs={len(msgs):4d} "
                  f"overlap_with_prior={ov}")
            seen_row_sigs.append(sigs)
        union = set().union(*seen_row_sigs) if seen_row_sigs else set()
        print(f"  SUM msgs={all_msgs}  UNIQUE sigs={len(union)}  "
              f"=> {'CUMULATIVE/OVERLAP' if all_msgs > len(union)*1.2 else 'DISJOINT'}")


if __name__ == "__main__":
    main()
