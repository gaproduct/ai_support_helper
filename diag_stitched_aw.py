"""Dump full stitched segments for the remaining awaiting_support tickets of a
day, with per-ticket last-message analysis: did the LAST client substantive get
a support reply inside the ticket window?"""
import sys
from datetime import date

from database import get_session
import resolution_metrics as rm
import ticket_slices as ts
from slice_stitched import stitched_tickets_for_day

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 7, 17)
    s = get_session()
    tickets = stitched_tickets_for_day(s, day)
    aw = [(cid, m, seg) for cid, m, seg in tickets if m.status == "awaiting_support"]
    print(f"{day}: {len(aw)} awaiting_support (stitched)\n")
    for cid, m, seg in aw:
        anchor = next((x for x in seg if x.is_client and x.substantive), None)
        last_sub = next((x for x in reversed(seg) if x.substantive), None)
        sup_subs = [x for x in seg if (not x.is_client) and x.substantive]
        t0 = anchor.ts.astimezone(rm.MSK)
        print("=" * 74)
        print(f"[{cid}] anchor {t0:%m-%d %H:%M %a}  msgs={len(seg)}  "
              f"support_substantive={len(sup_subs)}")
        print(f"  last_substantive = {'КЛИЕНТ' if last_sub.is_client else 'САППОРТ'} "
              f"@ {last_sub.ts.astimezone(rm.MSK):%m-%d %H:%M}")
        for x in seg:
            if not x.substantive:
                continue
            role = "КЛ " if x.is_client else "СП "
            t = x.ts.astimezone(rm.MSK)
            print(f"   {role}[{t:%m-%d %H:%M}] {(x.text or '').replace(chr(10),' ')[:120]}")


if __name__ == "__main__":
    main()
