"""Dump content of the awaiting_support tickets that have NO owner (operator never
answered), for the same cleaned stitched cohort as operator_unresolved.py."""
import sys
from collections import defaultdict
from datetime import date, timedelta

from database import get_session, Dialog
import resolution_metrics as rm
from slice_stitched import stitched_tickets_for_day, stitched_timeline
from shadow_dupes import shadow_dialog_ids
from operator_stats import build_ts_op_map


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def main():
    if len(sys.argv) > 2:
        a = date.fromisoformat(sys.argv[1]); b = date.fromisoformat(sys.argv[2])
    else:
        a, b = date(2026, 7, 16), date(2026, 7, 27)
    s = get_session()
    excl = shadow_dialog_ids(s)

    rows = []
    for day in daterange(a, b):
        cache = {}
        for cid, m, seg in stitched_tickets_for_day(s, day):
            if m.status != "awaiting_support":
                continue
            if cid not in cache:
                cache[cid] = build_ts_op_map(stitched_timeline(s, cid, excl))
            tsmap = cache[cid]
            sup = [x for x in seg if x.is_support and x.substantive]
            has_owner = any(tsmap.get(x.ts) for x in sup)
            if has_owner:
                continue
            cli = [x for x in seg if x.is_client and x.substantive]
            anchor = cli[0] if cli else None
            last = seg[-1] if seg else None
            n_sup_any = sum(1 for x in seg if x.is_support)
            ats = anchor.ts.astimezone(rm.MSK) if anchor else None
            rows.append({
                "day": day,
                "when": ats.strftime("%m-%d %H:%M") if ats else "?",
                "cid": cid[:12],
                "n_cli": len(cli),
                "n_sup_any": n_sup_any,
                "first": (anchor.text or "").replace("\n", " ")[:140] if anchor else "",
                "last_role": "client" if (last and last.is_client) else "support",
                "last": (last.text or "").replace("\n", " ")[:100] if last else "",
            })

    print(f"Всего без владельца: {len(rows)}\n")
    for r in sorted(rows, key=lambda x: x["day"]):
        print(f"[{r['when']}] cli={r['n_cli']} sup={r['n_sup_any']} last={r['last_role']}")
        print(f"    Q: {r['first']}")
        print(f"    L: {r['last']}")
    # crude bucketing by keyword
    buckets = defaultdict(int)
    KW = {
        "верификац": "верификация/разблокировка",
        "разблок": "верификация/разблокировка",
        "выплат": "выплаты",
        "пополн": "пополнение баланса",
        "оплат": "оплата/платёж",
        "документ": "документы",
        "акт": "документы",
        "счет": "документы",
        "счёт": "документы",
    }
    for r in rows:
        q = r["first"].lower()
        hit = None
        for k, v in KW.items():
            if k in q:
                hit = v
                break
        buckets[hit or "прочее/приветствие"] += 1
    print("\nПо темам (по первому сообщению):")
    for k, v in sorted(buckets.items(), key=lambda x: -x[1]):
        print(f"   {v:3d}  {k}")


if __name__ == "__main__":
    main()
