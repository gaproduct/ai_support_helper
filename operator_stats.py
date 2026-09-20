"""Per-operator statistics over the cleaned stitched cohort (shadows + non-tickets
excluded) for given day(s). Operator identity:
  • flomni:  author_id  = «...@madetask.team»
  • chatapp: operator_email (skip operator_is_bot)
Metrics per operator: tickets touched, first-responses, resolutions, support msgs,
distinct clients, median/avg first-response time on their first-responded tickets.
"""
import json
import sys
import statistics
from collections import defaultdict
from datetime import date

from database import get_session
import resolution_metrics as rm
from slice_stitched import stitched_tickets_for_day, stitched_timeline
from shadow_dupes import shadow_dialog_ids


def op_email(m: dict):
    if m.get("operator_is_bot"):
        return None
    for k in ("operator_email", "author_id", "author"):
        v = (m.get(k) or "").strip().lower()
        if v.endswith("@madetask.team"):
            return v
    return None


def build_ts_op_map(merged: list) -> dict:
    """ts -> operator_email по исходящим сообщениям."""
    out = {}
    for m in merged:
        if m.get("direction") != "outbound":
            continue
        ts = rm._parse_time(m.get("time") or m.get("time_unix"))
        if ts is None:
            continue
        oe = op_email(m)
        if oe:
            out[ts] = oe
    return out


def main():
    days = [date.fromisoformat(a) for a in sys.argv[1:]] or [date(2026, 7, 16), date(2026, 7, 17)]
    s = get_session()
    excl = shadow_dialog_ids(s)

    # агрегаты
    touched = defaultdict(set)      # op -> set(ticket_key)
    firstresp = defaultdict(int)    # op -> кол-во тикетов, где он ПЕРВЫМ ответил
    resolved = defaultdict(int)     # op -> кол-во решённых им тикетов
    msgs = defaultdict(int)         # op -> содержательных ответов
    clients = defaultdict(set)      # op -> set(client_id)
    fr_secs = defaultdict(list)     # op -> список first-response секунд
    res_secs = defaultdict(list)    # op -> список resolution секунд (по решённым им)

    tickets_total = 0
    tickets_with_support = 0

    for day in days:
        # кэш ts->op по клиентам за день
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
            if ops_here:
                tickets_with_support += 1
                # первый ответивший
                ops_here.sort()
                first_op = ops_here[0][1]
                firstresp[first_op] += 1
                if m.first_response_seconds is not None:
                    fr_secs[first_op].append(m.first_response_seconds)
                # резолюция, если решена и её время совпало с сообщением оператора
                if m.resolved_at is not None:
                    ro = tsmap.get(m.resolved_at)
                    if ro:
                        resolved[ro] += 1
                        if m.resolution_seconds is not None:
                            res_secs[ro].append(m.resolution_seconds)

    all_ops = sorted(touched, key=lambda o: (-firstresp[o], -len(touched[o]), o))
    print(f"Дни: {', '.join(d.isoformat() for d in days)}")
    print(f"Тикетов всего (очищенная когорта): {tickets_total}; с ответом оператора: {tickets_with_support}")
    print(f"Операторов: {len(all_ops)}\n")
    hdr = f"{'operator':16s} {'owned':>5s} {'resolved':>8s} {'FR med':>8s} {'RES med':>9s} {'msgs':>5s}"
    print(hdr); print("-" * len(hdr))
    for o in all_ops:
        fr = statistics.median(fr_secs[o]) if fr_secs[o] else None
        rs = statistics.median(res_secs[o]) if res_secs[o] else None
        print(f"{o.split('@')[0]:16s} {firstresp[o]:5d} {resolved[o]:8d} "
              f"{rm._fmt(fr):>8s} {rm._fmt(rs):>9s} {msgs[o]:5d}")


if __name__ == "__main__":
    main()
