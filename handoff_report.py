"""Handoff-time report over the cleaned stitched cohort for a date range.

Handoff = support passes the question to an adjacent department (compliance /
finance / documents / bank_provider / technical). resolution_metrics detects the
open ("передали в комплаенс …") and close ("коллеги подтвердили …") events, so a
handoff's duration = closed_at − opened_at (None while still open → the ticket
sits in pending_department).

Per department we report: how many handoffs, how many closed vs still-open, the
MEAN and median closed duration, and a few example request contexts (the client
anchor that led to the handoff)."""
import sys
import statistics
from collections import defaultdict
from datetime import date, time, timedelta

from database import get_session
import resolution_metrics as rm
from slice_stitched import stitched_tickets_for_day

# График смежных отделов (коллег): Пн–Пт 09:00–18:00 МСК.
HANDOFF_START = time(9, 0)
HANDOFF_END = time(18, 0)
HANDOFF_DAYS = {0, 1, 2, 3, 4}


def handoff_working_seconds(h) -> float:
    return rm.working_seconds_sched(h.opened_at, h.closed_at,
                                    HANDOFF_START, HANDOFF_END, HANDOFF_DAYS)

DEPT_RU = {
    "compliance": "Комплаенс / служба безопасности",
    "finance": "Финансовый / бухгалтерия",
    "documents": "Документооборот",
    "legal": "Юридический",
    "bank_provider": "Банк / платёжный провайдер",
    "technical": "Технический / разработка",
}


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def main():
    if len(sys.argv) > 2:
        a = date.fromisoformat(sys.argv[1]); b = date.fromisoformat(sys.argv[2])
    else:
        a, b = date(2026, 7, 16), date(2026, 7, 27)
    days = list(daterange(a, b))
    s = get_session()

    closed_secs = defaultdict(list)   # dept -> [seconds] по закрытым передачам
    n_total = defaultdict(int)        # dept -> всего передач
    n_open = defaultdict(int)         # dept -> ещё открытых (висят в отделе)
    ctx = defaultdict(list)           # dept -> [пример контекста запроса]
    tickets_with_handoff = 0
    total_handoffs = 0

    for day in days:
        for cid, m, seg in stitched_tickets_for_day(s, day):
            if not m.handoffs:
                continue
            tickets_with_handoff += 1
            anchor = next((x for x in seg if x.is_client and x.substantive), None)
            actext = (anchor.text or "").replace("\n", " ").strip() if anchor else ""
            for h in m.handoffs:
                total_handoffs += 1
                n_total[h.department] += 1
                if h.closed_at is None:
                    n_open[h.department] += 1
                else:
                    closed_secs[h.department].append(handoff_working_seconds(h))
                if actext and len(ctx[h.department]) < 6:
                    ctx[h.department].append(actext[:100])

    print(f"Период: {a.isoformat()} … {b.isoformat()}  ({len(days)} дн.)")
    print(f"График отделов: Пн–Пт {HANDOFF_START:%H:%M}–{HANDOFF_END:%H:%M} МСК (рабочее время)")
    print(f"Тикетов с передачей в отдел: {tickets_with_handoff}; всего передач: {total_handoffs}\n")

    order = sorted(n_total, key=lambda d: -n_total[d])
    hdr = f"{'отдел':32s} {'передач':>7s} {'сред.раб.вр':>11s}"
    print(hdr); print("-" * len(hdr))
    all_closed = []
    for dep in order:
        secs = closed_secs[dep]
        all_closed += secs
        mean = statistics.mean(secs) if secs else None
        print(f"{DEPT_RU.get(dep, dep):32s} {n_total[dep]:7d} "
              f"{rm._fmt(mean):>11s}")
    print("-" * len(hdr))
    gmean = statistics.mean(all_closed) if all_closed else None
    print(f"{'ИТОГО (закрытые)':32s} {total_handoffs:7d} "
          f"{rm._fmt(gmean):>11s}")

    print("\n" + "=" * 74)
    print("ПРИМЕРНЫЙ КОНТЕКСТ ЗАПРОСОВ ПО ОТДЕЛАМ\n")
    for dep in order:
        print(f"### {DEPT_RU.get(dep, dep)}  (передач: {n_total[dep]})")
        for c in ctx[dep]:
            print(f"   • {c}")
        print()


if __name__ == "__main__":
    main()
