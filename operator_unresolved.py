"""Per-operator stats over the cleaned stitched cohort for a date range, PLUS
for every operator the list of tickets they OWN (gave first response) that ended
`awaiting_support` — i.e. not completed — each with a deep-link to the dialog.

Owner of a ticket = operator who gave the first substantive support reply.
Unresolved = status == 'awaiting_support' on the stitched timeline (hard-window
slicing already applied). Links:
  • flomni : https://my.flomni.com/dialogs/search?receiver=<client_id sans -xx>
  • chatapp: https://new.dialogs.pro/dialogs/<license>/<messenger>/<chat_id>
"""
import re
import sys
import statistics
from collections import defaultdict
from datetime import date, time, timedelta

from database import get_session, Dialog
import resolution_metrics as rm
from slice_stitched import stitched_tickets_for_day, stitched_timeline
from shadow_dupes import shadow_dialog_ids
from operator_stats import op_email, build_ts_op_map

_LANG_SUFFIX = re.compile(r"-[a-z]{2}$")

# График саппорта: 7/7 08:00–21:00 МСК.
SUP_START = time(8, 0)
SUP_END = time(21, 0)
SUP_DAYS = {0, 1, 2, 3, 4, 5, 6}


def sup_working_seconds(start, end) -> float:
    if start is None or end is None:
        return None
    return rm.working_seconds_sched(start, end, SUP_START, SUP_END, SUP_DAYS)


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def link_for(cid: str, meta: dict) -> str:
    src = (meta or {}).get("source")
    if src == "flomni":
        receiver = _LANG_SUFFIX.sub("", cid)
        return f"https://my.flomni.com/dialogs/search?receiver={receiver}"
    if src == "chatapp":
        lic = (meta or {}).get("license_id") or "?"
        mt = (meta or {}).get("messenger_type") or "?"
        return f"https://new.dialogs.pro/dialogs/{lic}/{mt}/{cid}"
    return f"(source={src}) client_id={cid}"


def main():
    if len(sys.argv) > 2:
        a = date.fromisoformat(sys.argv[1]); b = date.fromisoformat(sys.argv[2])
    else:
        a, b = date(2026, 7, 16), date(2026, 7, 27)
    days = list(daterange(a, b))
    s = get_session()
    excl = shadow_dialog_ids(s)

    # client_id -> meta for links (source/license/messenger)
    meta = {}
    for r in s.query(Dialog).all():
        if r.client_id not in meta:
            meta[r.client_id] = {"source": r.source,
                                 "license_id": r.license_id,
                                 "messenger_type": r.messenger_type}

    touched = defaultdict(set)
    firstresp = defaultdict(int)
    resolved = defaultdict(int)
    msgs = defaultdict(int)
    clients = defaultdict(set)
    fr_secs = defaultdict(list)
    res_secs = defaultdict(list)
    # owner -> list of (day, cid, anchor_ts, link) for awaiting_support tickets
    unresolved = defaultdict(list)
    unowned = []  # awaiting_support with no identifiable operator (never answered)

    tickets_total = 0
    tickets_with_support = 0

    for day in days:
        opmap_cache = {}
        for cid, m, seg in stitched_tickets_for_day(s, day):
            tickets_total += 1
            tkey = (cid, m.started_at.isoformat() if m.started_at else str(tickets_total))
            if cid not in opmap_cache:
                opmap_cache[cid] = build_ts_op_map(stitched_timeline(s, cid, excl))
            tsmap = opmap_cache[cid]

            sup = [x for x in seg if x.is_support and x.substantive]
            ops_here = []
            for x in sup:
                oe = tsmap.get(x.ts)
                if not oe:
                    continue
                msgs[oe] += 1
                touched[oe].add(tkey)
                clients[oe].add(cid)
                ops_here.append((x.ts, oe))

            owner = None
            if ops_here:
                tickets_with_support += 1
                ops_here.sort()
                owner = ops_here[0][1]
                firstresp[owner] += 1
                frw = sup_working_seconds(m.started_at, m.first_response_at)
                if frw is not None:
                    fr_secs[owner].append(frw)
                if m.resolved_at is not None:
                    ro = tsmap.get(m.resolved_at)
                    if ro:
                        resolved[ro] += 1
                        rsw = sup_working_seconds(m.started_at, m.resolved_at)
                        if rsw is not None:
                            res_secs[ro].append(rsw)

            if m.status == "awaiting_support":
                anchor = next((x for x in seg if x.is_client and x.substantive), None)
                ats = anchor.ts.astimezone(rm.MSK) if anchor else None
                lk = link_for(cid, meta.get(cid))
                rec = (day, cid, ats, lk)
                if owner:
                    unresolved[owner].append(rec)
                else:
                    unowned.append(rec)

    all_ops = sorted(touched, key=lambda o: (-firstresp[o], -len(touched[o]), o))
    print(f"Период: {a.isoformat()} … {b.isoformat()}  ({len(days)} дн.)")
    print(f"Тикетов всего (очищенная когорта): {tickets_total}; с ответом оператора: {tickets_with_support}")
    print(f"Операторов: {len(all_ops)}\n")
    hdr = f"{'operator':16s} {'First Response med':>18s} {'Resolution med':>15s} {'Messages Count':>15s}"
    print(hdr); print("-" * len(hdr))
    for o in all_ops:
        fr = statistics.median(fr_secs[o]) if fr_secs[o] else None
        rs = statistics.median(res_secs[o]) if res_secs[o] else None
        print(f"{o.split('@')[0]:16s} {rm._fmt(fr):>18s} {rm._fmt(rs):>15s} {msgs[o]:15d}")

    print("\n" + "=" * 74)
    print("НЕЗАВЕРШЁННЫЕ ТИКЕТЫ (awaiting_support), сгруппированы по владельцу\n")
    for o in all_ops:
        recs = unresolved[o]
        if not recs:
            continue
        print(f"### {o.split('@')[0]}  — {len(recs)} незавершённых")
        for day, cid, ats, lk in sorted(recs, key=lambda r: r[0]):
            when = ats.strftime("%m-%d %H:%M") if ats else "?"
            print(f"   [{when}] {lk}")
        print()

    if unowned:
        print(f"### без владельца (оператор так и не подключился) — {len(unowned)}")
        for day, cid, ats, lk in sorted(unowned, key=lambda r: r[0]):
            when = ats.strftime("%m-%d %H:%M") if ats else "?"
            print(f"   [{when}] {lk}")


if __name__ == "__main__":
    main()
