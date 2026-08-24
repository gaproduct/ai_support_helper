"""Отчёт по менеджерам поддержки за месяц.

Инцидент = единица работы (resolution_metrics режет диалог по паузе 24ч).
Оператор берётся из сырых сообщений, метрики считаются тем же кодом, что и
ночная джоба, поэтому цифры сходятся с ticket_resolution_metrics.

Выборка ограничена методологией E. За июнь в tickets остались ещё и строки
методологии D от 14 июля: они протухли и тянут 49 диалогов, которые актуальная
методология уже не считает тикетами.

Инцидент засчитывается каждому, кто в нём работал. Поэтому колонка «инц» в сумме
больше числа инцидентов за месяц, а время смежных отделов нельзя складывать
между менеджерами: у общего инцидента оно приписано обоим.

  python manager_report.py --month 2026-08
"""
import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import text

from database import get_session
import resolution_metrics as rm

# Сотрудники chatapp опознаются числовым author_id, почты у них в сообщении нет.
# Ростер подтверждён вручную, тот же, что в resolution_metrics.SUPPORT_AGENT_IDS.
AGENT_BY_ID = {
    "470130440": "oksana",
    "5110608275": "andrei_s",
    "7289441632": "cathie",
    "7955379769": "elena",
    "8098672082": "fbp_support",
    "311830983": "alena",
}

# Общий аккаунт поддержки. За ним стоит живой человек, но кто именно, неизвестно.
# Держим отдельной строкой, иначе его работа просто исчезнет из отчёта.
SHARED_ID = "7692309574"
SHARED_LABEL = "RemozoSupport*"

# Один человек под разными адресами.
ALIAS = {"cathie@remozo.com": "cathie"}


def op_identity(m: dict) -> str | None:
    """Кто из сотрудников написал сообщение. None, если бот или неизвестно."""
    if m.get("operator_is_bot"):
        return None
    # Личная почта самая точная: у chatapp author_id общий на всю поддержку,
    # а operator_email указывает на конкретного сотрудника.
    for k in ("operator_email", "author_id", "author"):
        v = m.get(k)
        if not isinstance(v, str):
            continue
        v = v.strip().lower()
        if "@" in v and not v.startswith("@"):
            return ALIAS.get(v, v)
    aid = str(m.get("author_id") or "").strip()
    if aid in AGENT_BY_ID:
        return AGENT_BY_ID[aid]
    if aid == SHARED_ID:
        return SHARED_LABEL
    return None


def short(name: str) -> str:
    return name.split("@")[0]


def build_ts_map(raw: list) -> tuple[dict, Counter]:
    """ts(iso) -> оператор. Плюс счётчик неоднозначностей."""
    acc = defaultdict(set)
    for m in raw:
        if not isinstance(m, dict):
            continue
        ts = rm._parse_time(m.get("time") if m.get("time") not in (None, "") else m.get("time_unix"))
        if ts is None:
            continue
        who = op_identity(m)
        if who:
            acc[ts].add(who)
    amb = Counter()
    out = {}
    for ts, who in acc.items():
        if len(who) > 1:
            amb["ambiguous"] += 1
        out[ts] = sorted(who)[0]
    return out, amb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    args = ap.parse_args()
    y, mo = (int(x) for x in args.month.split("-"))

    # Границы месяца: от первого числа включительно до первого числа следующего
    # месяца строго. Так не нужно знать длину месяца и помнить про високосный год.
    lo = date(y, mo, 1)
    hi = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)

    with get_session() as db:
        rows = db.execute(text("""
            SELECT d.id, d.client_id, d.source, d.messages_json
            FROM dialogs d
            WHERE d.messages_json IS NOT NULL
              AND d.messages_json <> '' AND d.messages_json <> '[]'
              -- Теневые дубли: тот же TG-чат, пришедший вторым каналом Flomni.
              -- Без этого фильтра часть обращений считается дважды.
              AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                              WHERE s.dialog_id = d.id)
              AND EXISTS (SELECT 1 FROM tickets t WHERE t.dialog_id = d.id
                          AND t.dialog_date >= :df AND t.dialog_date < :dt
                          AND t.methodology = 'E')
            ORDER BY d.id
        """), {"df": lo.isoformat(), "dt": hi.isoformat()}).fetchall()

    touched = defaultdict(int)
    firstresp = defaultdict(int)
    resolved = defaultdict(int)
    msgs = defaultdict(int)
    clients = defaultdict(set)
    fr_secs = defaultdict(list)
    res_secs = defaultdict(list)
    res_work = defaultdict(list)
    handoff_inc = defaultdict(int)
    handoff_secs = defaultdict(float)
    handoff_dept = defaultdict(lambda: defaultdict(float))
    handoff_dept_n = defaultdict(lambda: defaultdict(int))
    last_day = None
    status_by_op = defaultdict(Counter)
    domains = defaultdict(Counter)

    n_inc = 0
    fr_unknown = 0
    res_unknown = 0
    amb_total = Counter()

    for did, cid, source, mj in rows:
        try:
            raw = json.loads(mj or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, list):
            continue

        tsmap, amb = build_ts_map(raw)
        amb_total.update(amb)

        norm = rm.normalize(raw)
        for idx, seg in enumerate(rm.segment_incidents(norm)):
            met = rm.compute_incident(seg, idx)
            if met.started_at is None:
                continue
            if (met.started_at.year, met.started_at.month) != (y, mo):
                continue
            n_inc += 1
            d = met.started_at.date()
            last_day = d if last_day is None or d > last_day else last_day

            # кто работал в инциденте
            here = set()
            for x in seg:
                if not (x.is_support and x.substantive):
                    continue
                who = tsmap.get(x.ts)
                if not who:
                    continue
                if x.text.strip().startswith("/"):
                    continue  # внутренняя команда, не ответ клиенту
                here.add(who)
                msgs[who] += 1
                clients[who].add(cid)
            for who in here:
                touched[who] += 1
                if "@" in who:
                    domains[short(who)][who.split("@")[1]] += 1
                status_by_op[who][met.status] += 1
                if met.handoff_seconds_total:
                    handoff_inc[who] += 1
                    handoff_secs[who] += met.handoff_seconds_total
                    for dep, sec in (met.handoff_seconds_by_dept or {}).items():
                        handoff_dept[who][dep] += sec
                        handoff_dept_n[who][dep] += 1

            if met.first_response_at is not None:
                who = tsmap.get(met.first_response_at)
                if who:
                    firstresp[who] += 1
                    if met.first_response_seconds is not None:
                        fr_secs[who].append(met.first_response_seconds)
                else:
                    fr_unknown += 1

            if met.resolved_at is not None:
                who = tsmap.get(met.resolved_at)
                if who:
                    resolved[who] += 1
                    if met.resolution_seconds is not None:
                        res_secs[who].append(met.resolution_seconds)
                    if met.resolution_working_seconds is not None:
                        res_work[who].append(met.resolution_working_seconds)
                else:
                    res_unknown += 1

    print(f"=== {args.month} ===")
    print(f"диалогов в выборке: {len(rows)}   инцидентов в месяце: {n_inc}")
    print(f"последний день с данными: {last_day}")
    print(f"первый ответ без опознанного оператора: {fr_unknown}")
    print(f"решение без опознанного оператора: {res_unknown}")
    if amb_total:
        print(f"меток времени с несколькими операторами: {amb_total['ambiguous']}")
    print()

    ops = sorted(touched, key=lambda o: (-touched[o], o))
    hdr = (f"{'менеджер':16s} {'инц':>4s} {'1-й отв':>7s} {'решил':>6s} "
           f"{'FRT мед':>9s} {'FRT ср':>9s} {'реш мед':>9s} {'раб мед':>9s} "
           f"{'передач':>7s} {'сообщ':>6s} {'клиент':>6s}")
    print(hdr)
    print("-" * len(hdr))
    for o in ops:
        med = lambda a: statistics.median(a) if a else None
        avg = lambda a: statistics.fmean(a) if a else None
        print(f"{short(o):16s} {touched[o]:4d} {firstresp[o]:7d} {resolved[o]:6d} "
              f"{rm._fmt(med(fr_secs[o])):>9s} {rm._fmt(avg(fr_secs[o])):>9s} "
              f"{rm._fmt(med(res_secs[o])):>9s} {rm._fmt(med(res_work[o])):>9s} "
              f"{handoff_inc[o]:7d} {msgs[o]:6d} {len(clients[o]):6d}")

    print("\nстатусы инцидентов по менеджерам:")
    all_st = ["resolved", "awaiting_client", "awaiting_support", "pending_department", "open"]
    h2 = f"{'менеджер':16s} " + " ".join(f"{s[:9]:>10s}" for s in all_st)
    print(h2); print("-" * len(h2))
    for o in ops:
        print(f"{short(o):16s} " + " ".join(f"{status_by_op[o][s]:10d}" for s in all_st))

    print("\nвремя ожидания смежных отделов (сумма / инцидентов):")
    deps = sorted({d for v in handoff_dept.values() for d in v})
    if deps:
        h3 = f"{'менеджер':16s} " + " ".join(f"{d[:13]:>15s}" for d in deps) + f"{'ИТОГО':>12s}"
        print(h3); print("-" * len(h3))
        for o in ops:
            if not handoff_dept[o]:
                continue
            cells = []
            for d in deps:
                sec = handoff_dept[o].get(d)
                cells.append(f"{rm._fmt(sec) + '/' + str(handoff_dept_n[o][d]):>15s}" if sec else f"{'—':>15s}")
            print(f"{short(o):16s} " + " ".join(cells) + f"{rm._fmt(handoff_secs[o]):>12s}")
        tot = defaultdict(float); totn = defaultdict(int)
        for o in handoff_dept:
            for d, sec in handoff_dept[o].items():
                tot[d] += sec; totn[d] += handoff_dept_n[o][d]
        print("\nсводно по отделам:")
        for d in sorted(tot, key=lambda x: -tot[x]):
            print(f"  {d:16s} {rm._fmt(tot[d]):>10s} за {totn[d]:3d} передач, "
                  f"в среднем {rm._fmt(tot[d]/totn[d])}")

    multi = {k: v for k, v in domains.items() if len(v) > 1}
    if multi:
        print("\nодин человек под несколькими доменами:")
        for k, v in multi.items():
            print(f"  {k}: {dict(v)}")


if __name__ == "__main__":
    main()
