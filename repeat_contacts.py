"""
Повторные обращения.

Метрика отвечает на вопрос, который не видят ни скорость первого ответа, ни
отметка «решено»: обращение закрыли, а человек вернулся с тем же вопросом.
Быстрый ответ, который не помог, здесь наконец становится виден.

Как считаем.

База это ЗАКРЫТЫЕ инциденты (status = resolved) из ticket_resolution_metrics.
Незакрытые не берём: они ещё не закончились, возвращаться некуда.

Повтор это новый инцидент того же клиента (source + client_id), начавшийся
после resolved_at предыдущего, не позже WINDOW_DAYS и в той же категории.
Категорию берём эффективную, как в отчёте: COALESCE(ручной оверрайд,
депозитный carve-out, категория тикета). Иначе цифра разойдётся с остальными
разделами.

Дальше три поправки, без которых метрика врёт.

1. Правый край данных. У обращения, закрытого за три дня до конца выгрузки,
   физически нет 14 дней на возврат. Оставить его в базе значит занизить долю,
   причём по-разному в разные месяцы. Поэтому база обрезается по последнему дню,
   у которого окно успело пройти целиком. Считаем от конца ВСЕХ данных, а не от
   конца периода: для июня при данных до 24 августа окно у всего месяца полное,
   обрезать нечего. Возвраты ищем по всей истории, включая инциденты после
   date_to.

2. Возврат на следующий день. Инциденты режутся по суточной паузе, поэтому один
   разговор, растянутый на два дня, выглядит как обращение и повтор. Это не
   повтор, это та же незаконченная история. Возврат считаем только через
   MIN_LAG_HOURS после закрытия.

3. Обрывки. Сегментация оставляет куски вроде «Ожидаю» или «есть новости?».
   Это не самостоятельное обращение и не повторный запрос, а хвост переписки.
   Поэтому и база, и возврат обязаны содержать реальный запрос клиента длиной
   не меньше MIN_STATEMENT_CHARS.

Что пробовали и не взяли. Сверку по смыслу через dialog_embeddings: требовать
косинус не ниже 0.4 между текстами базового обращения и возврата. На июне-августе
это сдвинуло долю с 23.4% на 22.7%, то есть в пределах шума. Лишнюю зависимость
от того, посчитаны ли векторы за период, взяли и выбросили. Если поддержка
начнёт спорить про конкретные пары внутри широкой категории «Выплаты», проверку
можно вернуть.

Чего метрика не ловит. Человек, пришедший с тем же вопросом под другим client_id
(другой мессенджер, другой кабинет), считается новым. Кросс-кабинетные дубли
частично снимает compute_tickets, остальное остаётся погрешностью вниз.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from statistics import median

from sqlalchemy import text

import fine_subcategory as fs
import resolution_metrics as rm
from database import engine

WINDOW_DAYS = 14
QUICK_WINDOW_DAYS = 3
MIN_LAG_HOURS = 24
MIN_STATEMENT_CHARS = 80
# Категории с меньшим числом закрытых обращений в разрезе не показываем: на
# десятке наблюдений доля скачет на десятки процентов и вводит в заблуждение.
MIN_CATEGORY_BASE = 15


# Инциденты берём только из отчётного набора тикетов (methodology E), чтобы база
# совпадала с остальным отчётом. Диалоги-близнецы Flomni исключаем: это та же
# переписка под вторым client_id, она раздувает базу и портит долю.
_SQL = """
SELECT m.dialog_id,
       m.incident_index,
       m.source,
       m.client_id,
       m.started_at,
       m.resolved_at,
       m.status,
       COALESCE(ovr.category, dep.eff, t.category) AS category,
       d.messages_json
  FROM ticket_resolution_metrics m
  JOIN tickets t
    ON t.dialog_id = m.dialog_id AND t.methodology = 'E'
  JOIN dialogs d
    ON d.id = m.dialog_id
  LEFT JOIN (SELECT dialog_id, :depcat AS eff
               FROM dialog_fine_subcategory
              WHERE fine_bucket = :depbucket) dep
    ON dep.dialog_id = m.dialog_id
  LEFT JOIN manual_category_override ovr
    ON ovr.dialog_id = m.dialog_id
 WHERE m.started_at IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                    WHERE s.dialog_id = m.dialog_id)
 ORDER BY m.source, m.client_id, m.started_at
"""


@dataclass
class Incident:
    dialog_id: int
    incident_index: int
    source: str
    client_id: str
    started_at: datetime
    resolved_at: datetime | None
    status: str
    category: str | None
    statement_chars: int


def _statement_chars(payload: str | None) -> int:
    """Объём того, что клиент написал своими словами, без заглушек и опросов.

    Меряем по диалогу целиком, а не по инциденту: у обрывков собственного
    содержательного текста нет в любом случае, а лишний разбор не нужен.
    """
    try:
        raw = json.loads(payload or "[]")
    except (TypeError, ValueError):
        return 0
    if not isinstance(raw, list):
        return 0
    return sum(
        len(m.text.strip())
        for m in rm.normalize(raw)
        if m.is_client and m.substantive
    )


def load_incidents() -> list[Incident]:
    params = {"depcat": fs.DEPOSIT_CATEGORY, "depbucket": fs.DEPOSIT_BUCKET}
    with engine.connect() as conn:
        rows = conn.execute(text(_SQL), params).mappings().all()

    # Один диалог даёт несколько инцидентов, текст парсим по разу на диалог.
    chars: dict[int, int] = {}
    out: list[Incident] = []
    for r in rows:
        d = dict(r)
        payload = d.pop("messages_json")
        did = d["dialog_id"]
        if did not in chars:
            chars[did] = _statement_chars(payload)
        out.append(Incident(**d, statement_chars=chars[did]))
    return out


def _find_return(
    incidents: list[Incident], base: Incident, window_days: int
) -> Incident | None:
    """Первый повторный запрос того же клиента после закрытия base."""
    deadline = base.resolved_at + timedelta(days=window_days)
    earliest = base.resolved_at + timedelta(hours=MIN_LAG_HOURS)
    for inc in incidents:
        if inc.started_at > deadline:
            break  # список отсортирован по времени
        if inc.started_at < earliest:
            continue
        if inc.dialog_id == base.dialog_id and inc.incident_index == base.incident_index:
            continue
        if inc.category != base.category:
            continue
        if inc.statement_chars < MIN_STATEMENT_CHARS:
            continue
        return inc
    return None


def compute(
    date_from: str | date,
    date_to: str | date,
    window_days: int = WINDOW_DAYS,
    incidents: list[Incident] | None = None,
) -> dict:
    """Повторные обращения за период. Возвращает готовые к печати числа.

    incidents можно передать снаружи, чтобы не читать и не разбирать всю выгрузку
    заново на каждый месяц: отчёту нужно несколько периодов подряд.
    """
    if isinstance(date_from, str):
        date_from = date.fromisoformat(date_from)
    if isinstance(date_to, str):
        date_to = date.fromisoformat(date_to)

    incidents_all = load_incidents() if incidents is None else incidents
    data_end = max(i.started_at.date() for i in incidents_all)
    cutoff = min(date_to, data_end - timedelta(days=window_days))

    by_client: dict[tuple[str, str], list[Incident]] = defaultdict(list)
    for inc in incidents_all:
        by_client[(inc.source, inc.client_id)].append(inc)

    total = 0
    skipped_short = 0
    returned = 0
    returned_quick = 0
    lags: list[float] = []
    per_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [база, возвраты]

    for incidents in by_client.values():
        for base in incidents:
            if base.status != "resolved" or base.resolved_at is None:
                continue
            closed = base.resolved_at.date()
            if closed < date_from or closed > cutoff:
                continue
            if base.statement_chars < MIN_STATEMENT_CHARS:
                skipped_short += 1
                continue

            total += 1
            cat = base.category or "Без категории"
            per_category[cat][0] += 1

            ret = _find_return(incidents, base, window_days)
            if ret:
                returned += 1
                per_category[cat][1] += 1
                lags.append((ret.started_at - base.resolved_at).total_seconds() / 86400)
            if _find_return(incidents, base, QUICK_WINDOW_DAYS):
                returned_quick += 1

    categories = [
        {
            "category": cat,
            "closed": base_n,
            "returned": ret_n,
            "pct": round(100.0 * ret_n / base_n, 1),
        }
        for cat, (base_n, ret_n) in per_category.items()
        if base_n >= MIN_CATEGORY_BASE
    ]
    categories.sort(key=lambda c: (-c["pct"], -c["closed"]))

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "cutoff": cutoff.isoformat(),
        "window_days": window_days,
        "quick_window_days": QUICK_WINDOW_DAYS,
        "closed": total,
        "skipped_short": skipped_short,
        "returned": returned,
        "returned_quick": returned_quick,
        "pct": pct(returned),
        "pct_quick": pct(returned_quick),
        "median_lag_days": round(median(lags), 1) if lags else None,
        "categories": categories,
    }


def render_text(res: dict) -> str:
    lines = [
        f"Повторные обращения, {res['date_from']} — {res['date_to']}",
        f"База: {res['closed']} закрытых обращений."
        + (f" Закрыты не позже {res['cutoff']}, чтобы у каждого было полное окно"
           f" в {res['window_days']} дн." if res["cutoff"] < res["date_to"] else "")
        + f" Ещё {res['skipped_short']} отброшено как обрывки переписки.",
        "",
        f"Вернулись с тем же вопросом за {res['window_days']} дн.: "
        f"{res['pct']}%  ({res['returned']})",
        f"из них уже за {res['quick_window_days']} дн.: "
        f"{res['pct_quick']}%  ({res['returned_quick']})",
        f"Медиана возврата: {res['median_lag_days']} дн.",
        "",
        f"{'Категория':<45}{'Закрыто':>9}{'Вернулись':>11}{'Доля':>8}",
    ]
    for c in res["categories"]:
        lines.append(
            f"{c['category'][:45]:<45}{c['closed']:>9}{c['returned']:>11}{c['pct']:>7}%"
        )
    return "\n".join(lines)


# Рендер использует только те классы, которые есть в обоих отчётах (.card, table),
# остальное инлайном. Иначе блок разъезжается: у отчётов разные наборы стилей.
def render_html(res: dict, trend: list[dict] | None = None) -> str:
    """HTML-фрагмент блока. Заголовок h2 остаётся за отчётом: у них своя нумерация."""
    def kpi(value: str, label: str) -> str:
        return (
            '<div style="flex:1;min-width:120px">'
            f'<div style="font-size:26px;font-weight:700;letter-spacing:-.02em">{value}</div>'
            f'<div style="font-size:11px;color:#7F8C8D;margin-top:2px">{label}</div></div>'
        )

    o = ['<div class="card">']
    o.append('<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:16px">')
    o.append(kpi(f"{res['pct']}%", "вернулись с тем же вопросом за "
                                   f"{res['window_days']} дн."))
    o.append(kpi(str(res["returned"]), f"повторов из {res['closed']} закрытых"))
    o.append(kpi(f"{res['pct_quick']}%", f"вернулись уже за {res['quick_window_days']} дн."))
    o.append(kpi(f"{res['median_lag_days']} дн." if res["median_lag_days"] else "—",
                 "медиана возврата"))
    o.append("</div>")

    if trend:
        cells = "".join(
            f'<td style="text-align:center">{escape(t["label"])}<br>'
            f'<b style="font-size:17px">{t["pct"]}%</b>'
            f'<br><span style="color:#7F8C8D;font-size:11px">база {t["closed"]}</span></td>'
            for t in trend
        )
        o.append(f'<table style="margin-bottom:16px"><tbody><tr>{cells}</tr></tbody></table>')

    top = res["categories"][0]["pct"] if res["categories"] else 100
    rows = []
    for c in res["categories"]:
        width = 100 * c["pct"] / top if top else 0
        rows.append(
            f'<tr><td>{escape(c["category"])}</td>'
            f'<td style="text-align:right">{c["closed"]}</td>'
            f'<td style="text-align:right">{c["returned"]}</td>'
            f'<td style="text-align:right;font-weight:600">{c["pct"]}%</td>'
            f'<td style="width:38%"><div style="height:9px;border-radius:5px;'
            f'background:#E74C3C;width:{width:.0f}%"></div></td></tr>'
        )
    o.append(
        "<table><thead><tr><th>Категория</th><th>Закрыто</th><th>Вернулись</th>"
        f'<th>Доля</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )

    o.append(
        '<div style="font-size:11px;color:#7F8C8D;margin-top:12px;line-height:1.5">'
        f'База это закрытые обращения, у которых прошло полное окно в {res["window_days"]} дн.'
        + (f' Поэтому она обрывается на {res["cutoff"]}: у закрытых позже ещё есть время вернуться.'
           if res["cutoff"] < res["date_to"] else "")
        + " "
        f'Ещё {res["skipped_short"]} отброшено как обрывки переписки: реплики вроде «Ожидаю» '
        'самостоятельным обращением не считаются. Возврат засчитывается не раньше чем через '
        f'{MIN_LAG_HOURS} часа после закрытия, иначе один разговор, растянутый на два дня, '
        'выглядел бы как повтор. Человек, пришедший с тем же вопросом под другим ID, '
        'считается новым, поэтому реальная доля чуть выше.</div>'
    )
    o.append("</div>")
    return "".join(o)


def main() -> None:
    ap = argparse.ArgumentParser(description="Повторные обращения за период")
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    args = ap.parse_args()
    print(render_text(compute(args.date_from, args.date_to, args.window)))


if __name__ == "__main__":
    main()
