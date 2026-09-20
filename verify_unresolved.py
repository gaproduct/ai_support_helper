"""Re-verify specific awaiting_support tickets: dump the stitched sub-ticket
segment (substantive messages, role, operator, time) for each (day, client_id)
pair, plus a verdict on WHY it is awaiting_support (what the last substantive
message is and whether any support reply follows it inside the window)."""
import sys
from datetime import date

from database import get_session
import resolution_metrics as rm
from slice_stitched import stitched_tickets_for_day, stitched_timeline
from shadow_dupes import shadow_dialog_ids
from operator_stats import build_ts_op_map

# (day, client_id) targets: operator-owned awaiting_support + anomalies
TARGETS = [
    ("2026-07-24", "c97a1255-4147-4437-aa21-a7a6832d3d62", "ekozlova"),
    ("2026-07-24", "fe0063ee-c673-4931-8c0f-5909406f29aa", "ekozlova"),
    ("2026-07-20", "41f7957f-1516-4952-b69b-1258dac5ca93", "tvakhitov"),
    ("2026-07-21", "d3a467d0-1fd3-4959-918a-6872f81f3e3a", "tvakhitov"),
    ("2026-07-21", "8663722647", "atatyanina"),
    ("2026-07-19", "2ce0a05a-ee5a-4bbf-b4af-d3dd879ab50c", "ayaglenko"),
    ("2026-07-22", "544f5709-b9d3-4cb2-8fbc-d670c2fbf2d1", "ayaglenko"),
    ("2026-07-23", "129cb0a3-0aec-4a93-a748-91acc4b7dce4", "ayaglenko"),
    ("2026-07-17", "5819035206", "mgazizova"),
    ("2026-07-22", "57d8c511-4d43-4ce0-b494-38563bd336d8", "sgavrilenko"),
    ("2026-07-22", "aaf9c299-b492-4187-9533-2db2cc986446", "ANOMALY x4 same-ts"),
]


def main():
    s = get_session()
    excl = shadow_dialog_ids(s)
    for day_s, cid, tag in TARGETS:
        day = date.fromisoformat(day_s)
        found = [(c, m, seg) for c, m, seg in stitched_tickets_for_day(s, day) if c.startswith(cid)]
        rawcid = found[0][0] if found else cid
        tsmap = build_ts_op_map(stitched_timeline(s, rawcid, excl))
        print("=" * 78)
        print(f"[{tag}] {cid}  day={day_s}  → sub-tickets anchored: {len(found)}")
        for c, m, seg in found:
            anchor = next((x for x in seg if x.is_client and x.substantive), None)
            last_sub = next((x for x in reversed(seg) if x.substantive), None)
            sup_subs = [x for x in seg if x.is_support and x.substantive]
            print(f"  --- status={m.status}  msgs={len(seg)}  support_substantive={len(sup_subs)}")
            print(f"      FR={rm._fmt(m.first_response_seconds)}  RES={rm._fmt(m.resolution_seconds)}")
            lr = "КЛИЕНТ" if (last_sub and last_sub.is_client) else "САППОРТ"
            lt = last_sub.ts.astimezone(rm.MSK) if last_sub else None
            print(f"      last_substantive = {lr} @ {lt:%m-%d %H:%M}" if lt else "      last_substantive = —")
            for x in seg:
                if not x.substantive:
                    continue
                role = "КЛ" if x.is_client else "СП"
                who = tsmap.get(x.ts, "")
                who = who.split("@")[0] if who else ""
                t = x.ts.astimezone(rm.MSK)
                print(f"        {role} [{t:%m-%d %H:%M}] {who:11s} {(x.text or '').replace(chr(10),' ')[:110]}")


if __name__ == "__main__":
    main()
