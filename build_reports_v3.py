"""Reports v3 — variant with structure:
  1) Aggregate stats first (heatmaps, charts)
  2) Narrative monthly analysis (free-form ticket descriptions)
  3) Top-company deep dives moved to the END
  4) Sample tickets within deep dive are taken from that company's most popular May categories

Outputs:
  - /app/reports/jan_may_2026/overview_jan_may_v3.html / .pdf
  - /app/reports/jan_may_2026/may_focus_v3.html / .pdf

Run: docker compose run --rm -v "$PWD:/app" scheduler python -u -m build_reports_v3
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from database import engine


log = logging.getLogger(__name__)

REPORTS_DIR = Path("/app/reports/jan_may_2026")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
MONTH_LABEL = {
    "2026-01": "Январь", "2026-02": "Февраль", "2026-03": "Март",
    "2026-04": "Апрель", "2026-05": "Май",
}
MAY = "2026-05"

# Компании, исключаемые из отчётов (служебные чаты, не клиентские тикеты).
EXCLUDED_COMPANIES = ("Systems",)
EXCLUDE_FILTER = "AND (company IS NULL OR company NOT IN ('Systems'))"


_RAT_PREFIX_RE = re.compile(
    r'^\s*клиент\s*[—\-–]\s*(заказчик|исполнитель|клиент)\s*[—\-–:,;]?\s*',
    re.IGNORECASE,
)


def _clean(s: str | None) -> str:
    if not s:
        return ""
    cleaned = _RAT_PREFIX_RE.sub("", s).strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned.replace("<", "&lt;")


def _fetch(sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        rs = conn.execute(text(sql), params or {})
        return [dict(r) for r in rs.mappings().all()]


def _heat(intensity: float) -> str:
    intensity = max(0.0, min(1.0, intensity))
    r = int(236 - (236 - 41)  * intensity)
    g = int(240 - (240 - 128) * intensity)
    b = int(241 - (241 - 185) * intensity)
    return f"rgb({r},{g},{b})"


def _side_label(s: str | None) -> str:
    return {"customer": "заказчик", "executor": "исполнитель"}.get(s or "", s or "")


# =====================================================================
# Shared building blocks
# =====================================================================

def _monthly_bars(monthly: list[dict]) -> str:
    if not monthly:
        return ""
    max_v = max(r["total"] for r in monthly) or 1
    bars = []
    bw, gap, ch = 110, 25, 220
    for i, r in enumerate(monthly):
        h = r["total"] / max_v * ch
        x = 50 + i * (bw + gap)
        y = 50 + ch - h
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bw}" height="{h}" fill="#3498DB" rx="4"/>'
            f'<text x="{x + bw/2}" y="{y - 8}" text-anchor="middle" fill="#2c3e50" '
            f'font-size="16" font-weight="600">{r["total"]}</text>'
            f'<text x="{x + bw/2}" y="{50 + ch + 22}" text-anchor="middle" '
            f'fill="#7f8c8d" font-size="13">{MONTH_LABEL[r["ym"]]}</text>'
        )
    return f'<svg viewBox="0 0 800 320" width="100%">{" ".join(bars)}</svg>'


def _side_stacked(monthly: list[dict]) -> str:
    rows = []
    for r in monthly:
        tot = r["customer"] + r["executor"] or 1
        cust_pct = r["customer"] / tot * 100
        exec_pct = r["executor"] / tot * 100
        rows.append(f'''
        <div class="srow">
          <div class="slabel">{MONTH_LABEL[r["ym"]]}</div>
          <div class="sbar">
            <div class="seg cust" style="width:{cust_pct}%">{r["customer"]}</div>
            <div class="seg exec" style="width:{exec_pct}%">{r["executor"]}</div>
          </div>
        </div>
        ''')
    return "".join(rows)


def _company_heatmap(top_companies: list[dict], cm: dict, months: list[str]) -> str:
    headers = "".join(f'<th>{MONTH_LABEL[m]}</th>' for m in months)
    max_v = max((cm.get((c["company"], m), 0) for c in top_companies for m in months),
                default=1)
    rows = []
    for i, c in enumerate(top_companies):
        cells = []
        for m in months:
            n = cm.get((c["company"], m), 0)
            intensity = n / max_v if max_v else 0
            bg = _heat(intensity)
            color = "#fff" if intensity > 0.5 else "#2c3e50"
            cells.append(f'<td style="background:{bg};color:{color}">{n or ""}</td>')
        rows.append(
            f'<tr><td class="rank">{i+1}</td><td class="cn">{c["company"]}</td>'
            f'{"".join(cells)}<td class="tot">{c["total"]}</td></tr>'
        )
    return f'''
    <table class="top15">
      <thead><tr><th>#</th><th>Компания</th>{headers}<th>Всего</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    '''


def _category_heatmap(top_cats: list[str], cat_by_month: dict, months: list[str]) -> str:
    """Heatmap: categories × months."""
    headers = "".join(f'<th>{MONTH_LABEL[m]}</th>' for m in months)
    max_v = max((cat_by_month.get((cat, m), 0) for cat in top_cats for m in months),
                default=1)
    rows = []
    for i, cat in enumerate(top_cats):
        cells = []
        row_total = 0
        for m in months:
            n = cat_by_month.get((cat, m), 0)
            row_total += n
            intensity = n / max_v if max_v else 0
            bg = _heat(intensity)
            color = "#fff" if intensity > 0.5 else "#2c3e50"
            cells.append(f'<td style="background:{bg};color:{color}">{n or ""}</td>')
        rows.append(
            f'<tr><td class="rank">{i+1}</td><td class="cn">{cat}</td>'
            f'{"".join(cells)}<td class="tot">{row_total}</td></tr>'
        )
    return f'''
    <table class="top15">
      <thead><tr><th>#</th><th>Категория</th>{headers}<th>Всего</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    '''


def _side_donut(customer: int, executor: int) -> str:
    """Compact SVG donut showing cust vs exec split."""
    tot = customer + executor or 1
    cust_pct = customer / tot * 100
    cust_dash = cust_pct * 2.512  # circumference ≈ 251.2 for r=40
    return f'''
    <svg viewBox="0 0 100 100" width="140" height="140">
      <circle cx="50" cy="50" r="40" fill="none" stroke="#27AE60" stroke-width="14"/>
      <circle cx="50" cy="50" r="40" fill="none" stroke="#E74C3C" stroke-width="14"
              stroke-dasharray="{cust_dash:.1f} 251.2" transform="rotate(-90 50 50)"/>
      <text x="50" y="48" text-anchor="middle" font-size="14" font-weight="700" fill="#2C3E50">{cust_pct:.0f}%</text>
      <text x="50" y="62" text-anchor="middle" font-size="6" fill="#7F8C8D">заказчики</text>
    </svg>
    '''


# =====================================================================
# Narrative analysis — month-by-month free-form descriptions
# =====================================================================

def _build_narrative(months: list[str], cat_month_n: dict[tuple[str, str], int],
                     cat_month_examples: dict[tuple[str, str], list[dict]]) -> str:
    """For each month: pick top-3 categories, write a paragraph, attach 1 example each."""
    blocks = []
    for m in months:
        # top categories for this month
        cats_in_month = [(c, n) for (m_, c), n in cat_month_n.items() if m_ == m]
        cats_in_month.sort(key=lambda x: -x[1])
        top3 = cats_in_month[:3]
        if not top3:
            continue

        total_m = sum(n for _, n in cats_in_month)
        # Compose narrative
        intro_parts = []
        for cat, n in top3:
            share = n / total_m * 100 if total_m else 0
            intro_parts.append(f"«{cat}» — {n} ({share:.0f}%)")

        category_blocks = []
        for cat, n in top3:
            examples = cat_month_examples.get((m, cat), [])[:2]
            ex_html = ""
            if examples:
                ex_items = "".join(
                    f"<li><span class='ex-meta'>{e['dialog_date']} · "
                    f"{_side_label(e.get('may_side'))} · "
                    f"{e.get('company') or '—'}</span>"
                    f"<div class='ex-body'>{_clean(e.get('rationale'))}</div>"
                    f"{'<a class=ex-link href=\"' + (e.get('chat_url') or '') + '\" target=_blank>чат →</a>' if e.get('chat_url') else ''}"
                    f"</li>"
                    for e in examples
                )
                ex_html = f"<ul class='examples'>{ex_items}</ul>"
            category_blocks.append(
                f"<div class='cat-block'>"
                f"<h5>{cat} <span class='cat-n'>· {n} тикетов</span></h5>"
                f"{ex_html}</div>"
            )

        blocks.append(f"""
        <div class="month-narrative">
          <h3>{MONTH_LABEL[m]}</h3>
          <p class="lead">Топ-3 темы месяца: {", ".join(intro_parts)}. Всего за месяц — {total_m} классифицированных тикетов.</p>
          {"".join(category_blocks)}
        </div>
        """)
    return "".join(blocks)


def _fetch_narrative_data(date_from: str, date_to: str) -> tuple[dict, dict]:
    """Return (cat_month_n, cat_month_examples)."""
    cats = _fetch("""
        SELECT to_char(dialog_date,'YYYY-MM') ym, may_category, COUNT(*) n
          FROM analytics_archive_jan_may
         WHERE company IS DISTINCT FROM 'Systems'
           AND dialog_date BETWEEN :a AND :b
           AND may_category IS NOT NULL
         GROUP BY 1,2
    """, {"a": date_from, "b": date_to})
    cat_month_n: dict[tuple[str, str], int] = {}
    for r in cats:
        cat_month_n[(r["ym"], r["may_category"])] = r["n"]

    # Get 2 example tickets per (month, category) with longest rationale
    ex = _fetch("""
        WITH ranked AS (
          SELECT to_char(dialog_date,'YYYY-MM') ym,
                 may_category, may_side, company, dialog_date,
                 LEFT(may_rationale, 220) AS rationale, chat_url,
                 ROW_NUMBER() OVER (
                   PARTITION BY to_char(dialog_date,'YYYY-MM'), may_category
                   ORDER BY LENGTH(COALESCE(may_rationale,'')) DESC, dialog_date
                 ) AS rn
            FROM analytics_archive_jan_may
           WHERE company IS DISTINCT FROM 'Systems'
             AND dialog_date BETWEEN :a AND :b
             AND may_category IS NOT NULL
             AND may_rationale IS NOT NULL AND may_rationale <> ''
        )
        SELECT * FROM ranked WHERE rn <= 2
    """, {"a": date_from, "b": date_to})
    cat_month_examples: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in ex:
        cat_month_examples[(r["ym"], r["may_category"])].append(r)
    return cat_month_n, cat_month_examples


# =====================================================================
# Company deep dive — samples from THAT company's most popular MAY categories
# =====================================================================

def _build_company_deep_dive(company: str, months: list[str]) -> str:
    """months: список месяцев YYYY-MM, по которым строится разбор. Сэмплы не выводятся."""
    monthly = _fetch("""
        SELECT to_char(dialog_date,'YYYY-MM') ym, COUNT(*) n,
               COUNT(*) FILTER (WHERE may_side='customer') AS cust,
               COUNT(*) FILTER (WHERE may_side='executor') AS exec_
          FROM analytics_archive_jan_may
         WHERE company = :c
           AND to_char(dialog_date,'YYYY-MM') = ANY(:months)
         GROUP BY 1 ORDER BY 1
    """, {"c": company, "months": months})

    cats = _fetch("""
        SELECT may_category, may_side, COUNT(*) n
          FROM analytics_archive_jan_may
         WHERE company = :c
           AND to_char(dialog_date,'YYYY-MM') = ANY(:months)
           AND may_category IS NOT NULL
         GROUP BY 1,2
    """, {"c": company, "months": months})
    cat_total_map: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in cats:
        cat_total_map[r["may_category"]][r["may_side"] or "other"] = r["n"]
    cat_sorted = sorted(
        cat_total_map.items(),
        key=lambda kv: -sum(kv[1].values()),
    )

    total = sum(m["n"] for m in monthly)
    cust = sum(m["cust"] for m in monthly)
    ex = sum(m["exec_"] for m in monthly)
    cust_share = cust / total * 100 if total else 0
    exec_share = ex / total * 100 if total else 0

    cat_rows = []
    for cat, d in cat_sorted[:12]:
        cu = d.get("customer", 0)
        ee = d.get("executor", 0)
        tot = cu + ee + d.get("other", 0)
        cat_rows.append(
            f"<tr><td>{cat}</td><td>{tot}</td>"
            f"<td>{cu}</td><td>{ee}</td></tr>"
        )

    if len(months) > 1:
        # Multi-month: показываем помесячную динамику + категории за период
        rows_m = "".join(
            f"<tr><td>{MONTH_LABEL.get(m['ym'], m['ym'])}</td><td>{m['n']}</td>"
            f"<td>{m['cust']}</td><td>{m['exec_']}</td></tr>"
            for m in monthly
        )
        body = f"""
      <div class="grid2">
        <div>
          <h4>Помесячная динамика</h4>
          <table class="d"><thead><tr><th>Месяц</th><th>Всего</th><th>Cust</th><th>Exec</th></tr></thead>
          <tbody>{rows_m}</tbody></table>
        </div>
        <div>
          <h4>Топ категорий за период</h4>
          <table class="d"><thead><tr><th>Категория</th><th>Всего</th><th>Cust</th><th>Exec</th></tr></thead>
          <tbody>{"".join(cat_rows)}</tbody></table>
        </div>
      </div>"""
    else:
        # Single month (May focus): только категории
        body = f"""
      <h4>Категории обращений</h4>
      <table class="d"><thead><tr><th>Категория</th><th>Всего</th><th>От заказчиков</th><th>От исполнителей</th></tr></thead>
      <tbody>{"".join(cat_rows)}</tbody></table>"""

    return f"""
    <div class="company-card">
      <h3>{company}</h3>
      <div class="stats">
        <div class="stat"><div class="val">{total}</div><div class="lbl">всего тикетов</div></div>
        <div class="stat"><div class="val">{cust_share:.0f}%</div><div class="lbl">от заказчика</div></div>
        <div class="stat"><div class="val">{exec_share:.0f}%</div><div class="lbl">от исполнителя</div></div>
      </div>
      {body}
    </div>
    """


# =====================================================================
# Shared CSS (both reports)
# =====================================================================

CSS_COMMON = """
@page { size: A4; margin: 18mm 12mm; }
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; color:#2C3E50; line-height:1.4; }
h1 { color:#1A2935; font-size:26px; margin:0 0 4px; }
h2 { color:#34495E; font-size:18px; border-left:4px solid #3498DB; padding-left:10px; margin:24px 0 12px; page-break-after: avoid; }
h3 { color:#2C3E50; font-size:16px; margin:18px 0 8px; }
h4 { color:#34495E; font-size:13px; margin:10px 0 6px; }
h5 { font-size:12px; color:#1A2935; margin:8px 0 4px; }
.hero { background:linear-gradient(135deg,#3498DB,#2C3E50); color:#fff; padding:18px 22px; border-radius:8px; margin-bottom:18px; }
.hero .sub { opacity:0.85; font-size:13px; }
.big-num { font-size:42px; font-weight:700; line-height:1; }
.kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:16px 0; }
.kpi { background:#fff; border:1px solid #E6EAEE; border-radius:6px; padding:10px 12px; text-align:center; }
.kpi .v { font-size:24px; font-weight:700; color:#3498DB; }
.kpi .l { font-size:10px; text-transform:uppercase; color:#7f8c8d; margin-top:2px; }
.card { border:1px solid #E6EAEE; border-radius:6px; padding:14px 16px; margin-bottom:14px; background:#fff; }
table { width:100%; border-collapse:collapse; font-size:11px; }
th { background:#34495E; color:#fff; padding:6px 8px; text-align:left; font-weight:600; }
td { padding:5px 8px; border-bottom:1px solid #ECF0F1; }
table.top15 td.rank { text-align:center; font-weight:700; color:#7f8c8d; width:30px; }
table.top15 td.cn { font-weight:600; max-width:220px; }
table.top15 td.tot { font-weight:700; background:#34495E; color:#fff; text-align:center; }
table.top15 td { text-align:center; }
.month-narrative { background:#FBFCFD; border:1px solid #E6EAEE; border-radius:8px; padding:14px 18px; margin-bottom:14px; }
.month-narrative h3 { font-size:18px; color:#1A2935; margin-top:0; border-bottom:2px solid #3498DB; padding-bottom:4px; display:inline-block; }
.month-narrative .lead { font-size:12.5px; color:#34495E; margin:8px 0 12px; line-height:1.55; }
.cat-block { background:#F8F9FA; border-left:3px solid #3498DB; padding:8px 12px; margin:8px 0; }
.cat-block h5 { margin:0 0 6px; font-size:12px; color:#1A2935; }
.cat-block .cat-n { color:#7F8C8D; font-weight:400; font-size:11px; }
ul.examples { margin:4px 0; padding-left:18px; font-size:11px; color:#34495E; }
ul.examples li { margin-bottom:6px; }
.ex-meta { font-size:10px; color:#7F8C8D; }
.ex-body { font-size:11.5px; color:#2C3E50; line-height:1.45; margin:2px 0; }
.ex-link { font-size:10px; color:#3498DB; text-decoration:none; font-weight:600; }
.company-card { background:#FBFCFD; border:1px solid #DCE3EA; border-radius:8px; padding:14px 18px; margin-bottom:18px; }
.company-card h3 { font-size:18px; margin-top:0; color:#1A2935; }
.stats { display:flex; gap:16px; margin:12px 0; }
.stat { flex:1; text-align:center; background:#fff; border:1px solid #E6EAEE; border-radius:6px; padding:10px; }
.stat .val { font-size:24px; font-weight:700; color:#3498DB; }
.stat .lbl { font-size:10px; color:#7f8c8d; text-transform:uppercase; margin-top:2px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:12px; }
table.d, table.samples { font-size:10px; }
table.samples td.rat { font-size:9.5px; color:#566573; max-width:300px; word-wrap:break-word; }
table.samples td a { color:#3498DB; text-decoration:none; font-weight:600; }
.cat-section { margin:10px 0; }
.cat-title { font-size:11px; color:#3498DB; text-transform:uppercase; letter-spacing:0.4px; margin:6px 0 4px; padding-bottom:2px; border-bottom:1px solid #ECF0F1; }
.srow { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
.slabel { width:80px; font-size:11px; font-weight:600; }
.sbar { flex:1; display:flex; height:24px; border-radius:4px; overflow:hidden; background:#ECF0F1; }
.seg { display:flex; align-items:center; justify-content:center; color:#fff; font-size:10px; font-weight:700; }
.seg.cust { background:#E74C3C; }
.seg.exec { background:#27AE60; }
.disclaimer { font-size:9px; color:#95A5A6; font-style:italic; margin-top:14px; }
.metrics-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.metric-card { background:#F8F9FA; border-radius:6px; padding:10px 12px; }
.metric-card h4 { margin-top:0; }
.legend { display:flex; gap:14px; font-size:11px; margin:8px 0 0; color:#7F8C8D; }
.legend .sw { display:inline-block; width:12px; height:12px; border-radius:2px; vertical-align:middle; margin-right:4px; }
"""


# =====================================================================
# REPORT 1 v3 — Overview Jan-May
# =====================================================================

def build_overview_v3() -> Path:
    log.info("Building overview_jan_may_v3")

    monthly = _fetch("""
        SELECT to_char(dialog_date,'YYYY-MM') ym, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE may_side='customer') AS customer,
               COUNT(*) FILTER (WHERE may_side='executor') AS executor
          FROM analytics_archive_jan_may
         WHERE company IS DISTINCT FROM 'Systems'
         GROUP BY 1 ORDER BY 1
    """)
    total_all = sum(r["total"] for r in monthly)
    cust_all = sum(r["customer"] for r in monthly)
    exec_all = sum(r["executor"] for r in monthly)

    # Top-15 companies
    top_companies = _fetch("""
        SELECT company, COUNT(*) AS total
          FROM analytics_archive_jan_may
         WHERE company IS NOT NULL AND company<>'' AND company <> 'Systems'
         GROUP BY company ORDER BY total DESC LIMIT 15
    """)
    top_names = [c["company"] for c in top_companies]
    cm_raw = _fetch("""
        SELECT company, to_char(dialog_date,'YYYY-MM') ym, COUNT(*) n
          FROM analytics_archive_jan_may
         WHERE company = ANY(:names)
         GROUP BY 1,2
    """, {"names": top_names})
    cm: dict[tuple[str, str], int] = {}
    for r in cm_raw:
        cm[(r["company"], r["ym"])] = r["n"]

    # Top categories overall + heatmap data
    cat_raw = _fetch("""
        SELECT may_category, to_char(dialog_date,'YYYY-MM') ym, COUNT(*) n
          FROM analytics_archive_jan_may
         WHERE may_category IS NOT NULL AND company IS DISTINCT FROM 'Systems'
         GROUP BY 1,2
    """)
    cat_total: dict[str, int] = defaultdict(int)
    cat_by_month: dict[tuple[str, str], int] = {}
    for r in cat_raw:
        cat_total[r["may_category"]] += r["n"]
        cat_by_month[(r["may_category"], r["ym"])] = r["n"]
    top_cat_names = [k for k, _ in sorted(cat_total.items(), key=lambda x: -x[1])[:12]]

    # Company deep dives (top-2) — без сэмплов
    top2 = top_names[:2]
    deep_dive = "\n".join(_build_company_deep_dive(c, MONTHS) for c in top2)

    monthly_chart = _monthly_bars(monthly)
    comp_heat = _company_heatmap(top_companies, cm, MONTHS)
    cat_heat = _category_heatmap(top_cat_names, cat_by_month, MONTHS)

    # MoM growth & weekend metrics
    wk = _fetch("""
        SELECT to_char(dialog_date,'YYYY-MM') ym,
               COUNT(*) FILTER (WHERE EXTRACT(ISODOW FROM dialog_date) IN (6,7)) AS weekend,
               COUNT(*) AS total
          FROM analytics_archive_jan_may
         WHERE company IS DISTINCT FROM 'Systems'
         GROUP BY 1 ORDER BY 1
    """)

    growth_rows = []
    for i, r in enumerate(monthly):
        if i == 0:
            growth = "—"
        else:
            prev = monthly[i-1]["total"]
            curr = r["total"]
            gpct = (curr - prev) / prev * 100 if prev else 0
            growth = f"{gpct:+.1f}%"
        growth_rows.append(
            f'<tr><td>{MONTH_LABEL[r["ym"]]}</td><td>{r["total"]}</td><td>{growth}</td></tr>'
        )
    wk_rows = []
    for r in wk:
        share = r["weekend"] / r["total"] * 100 if r["total"] else 0
        wk_rows.append(
            f'<tr><td>{MONTH_LABEL[r["ym"]]}</td>'
            f'<td>{r["weekend"]}</td><td>{r["total"] - r["weekend"]}</td>'
            f'<td>{share:.1f}%</td></tr>'
        )

    metrics_html = f'''
    <div class="metrics-grid">
      <div class="metric-card">
        <h4>Рост MoM</h4>
        <table><tr><th>Месяц</th><th>Тикетов</th><th>Δ</th></tr>{"".join(growth_rows)}</table>
      </div>
      <div class="metric-card">
        <h4>Выходные vs Будни</h4>
        <table><tr><th>Месяц</th><th>Вых.</th><th>Будни</th><th>%</th></tr>{"".join(wk_rows)}</table>
      </div>
    </div>
    '''

    may_count = next((r["total"] for r in monthly if r["ym"] == MAY), 0)

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Аналитика поддержки Январь–Май 2026 (v3)</title>
<style>{CSS_COMMON}</style></head><body>

<div class="hero">
  <h1>Аналитика поддержки · Январь – Май 2026</h1>
  <div class="sub">Сводный отчёт на базе ChatApp / Bitrix / Flomni</div>
  <div style="margin-top:14px"><span class="big-num">{total_all}</span>
  <span style="margin-left:10px">тикетов всего · {may_count} в мае · {cust_all} от заказчиков · {exec_all} от исполнителей</span></div>
</div>

<h2>1. Объём по месяцам</h2>
<div class="card">{monthly_chart}</div>

<h2>2. Heatmap: Топ-15 компаний × месяцы</h2>
<div class="card">{comp_heat}</div>

<h2>3. Heatmap: Топ-12 категорий × месяцы</h2>
<div class="card">{cat_heat}</div>

<h2>4. Ключевые метрики</h2>
{metrics_html}

<h2>5. Детализация по топ компаниям</h2>
<p style="color:#566573;font-size:12px;margin-bottom:8px">
Самые требовательные клиенты периода — динамика по месяцам и распределение категорий.</p>
{deep_dive}

<div class="disclaimer">
Методология: 1 тикет = 1 календарный день на чат (ChatApp); для запросов, поступивших
в пятницу после 18:00 / субботу / воскресенье — окно 72 ч (выходные). Bitrix/Flomni: 1 строка = 1 тикет.
</div>

</body></html>"""

    path = REPORTS_DIR / "overview_jan_may_v3.html"
    path.write_text(html, encoding="utf-8")
    log.info("HTML written: %s", path)
    return path


# =====================================================================
# REPORT 2 v3 — May focus
# =====================================================================

def build_may_v3() -> Path:
    log.info("Building may_focus_v3")

    summary = _fetch("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE may_side='customer') AS customer,
               COUNT(*) FILTER (WHERE may_side='executor') AS executor,
               COUNT(*) FILTER (WHERE source='chatapp') AS chatapp,
               COUNT(*) FILTER (WHERE source='bitrix')  AS bitrix,
               COUNT(*) FILTER (WHERE source='flomni')  AS flomni
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = :may AND company IS DISTINCT FROM 'Systems'
    """, {"may": MAY})[0]

    top_companies = _fetch("""
        SELECT company,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE may_side='customer') AS cust,
               COUNT(*) FILTER (WHERE may_side='executor') AS exec_
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = :may AND company IS DISTINCT FROM 'Systems'
           AND company IS NOT NULL AND company<>''
         GROUP BY company ORDER BY total DESC LIMIT 10
    """, {"may": MAY})

    cats = _fetch("""
        SELECT may_category, may_side, COUNT(*) n
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = :may AND company IS DISTINCT FROM 'Systems'
           AND may_category IS NOT NULL
         GROUP BY 1,2
    """, {"may": MAY})
    cat_total: dict[str, int] = defaultdict(int)
    cat_side: dict[tuple[str, str], int] = defaultdict(int)
    for r in cats:
        cat_total[r["may_category"]] += r["n"]
        cat_side[(r["may_category"], r["may_side"])] = r["n"]
    top_cats = sorted(cat_total.items(), key=lambda x: -x[1])

    # Top-5 deep dive — только данные мая, без сэмплов
    deep_dive = "\n".join(
        _build_company_deep_dive(c["company"], [MAY]) for c in top_companies[:5]
    )

    # Top-10 компаний мая для heatmap (категории × компании)
    top10_companies_rows = _fetch("""
        SELECT company, COUNT(*) AS total
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = :may AND company IS DISTINCT FROM 'Systems'
           AND company IS NOT NULL AND company<>''
         GROUP BY company ORDER BY total DESC LIMIT 10
    """, {"may": MAY})
    top10_names = [r["company"] for r in top10_companies_rows]
    top10_totals = {r["company"]: r["total"] for r in top10_companies_rows}
    # Top-10 категорий мая
    top_cat_may_names = [c for c, _ in top_cats[:10]]

    # Cell data: (company, category) -> count
    comp_cat_rows = _fetch("""
        SELECT company, may_category, COUNT(*) AS n
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = :may AND company IS DISTINCT FROM 'Systems'
           AND company = ANY(:cs) AND may_category = ANY(:ks)
         GROUP BY 1,2
    """, {"may": MAY, "cs": top10_names, "ks": top_cat_may_names})
    cc: dict[tuple[str, str], int] = {}
    for r in comp_cat_rows:
        cc[(r["company"], r["may_category"])] = r["n"]

    # Build transposed heatmap: rows = categories, columns = companies (top-10)
    max_v = max((cc.get((c, k), 0) for c in top10_names for k in top_cat_may_names),
                default=1)
    cc_headers = "".join(f'<th class="rot">{c}</th>' for c in top10_names)
    cc_rows_html = []
    for i, cat in enumerate(top_cat_may_names):
        cells = []
        row_tot = 0
        for comp in top10_names:
            n = cc.get((comp, cat), 0)
            row_tot += n
            intensity = n / max_v if max_v else 0
            bg = _heat(intensity)
            color = "#fff" if intensity > 0.5 else "#2c3e50"
            cells.append(f'<td style="background:{bg};color:{color}">{n or ""}</td>')
        cc_rows_html.append(
            f'<tr><td class="rank">{i+1}</td><td class="cn">{cat}</td>'
            f'{"".join(cells)}<td class="tot">{row_tot}</td></tr>'
        )
    # Footer with column totals
    foot_cells = "".join(f'<td class="tot">{top10_totals[c]}</td>' for c in top10_names)
    grand_total = sum(top10_totals.values())
    comp_cat_heatmap = f'''
    <table class="top15 cc-heatmap">
      <thead><tr><th>#</th><th>Категория</th>{cc_headers}<th>Всего</th></tr></thead>
      <tbody>{"".join(cc_rows_html)}</tbody>
      <tfoot><tr><td></td><td class="cn">Всего по компании</td>{foot_cells}<td class="tot">{grand_total}</td></tr></tfoot>
    </table>
    '''

    # Top companies KPI table
    top_rows = []
    max_v = max((c["total"] for c in top_companies), default=1)
    for i, c in enumerate(top_companies):
        bar_w = c["total"] / max_v * 100
        top_rows.append(
            f"<tr><td>{i+1}</td><td>{c['company']}</td>"
            f"<td>{c['total']}</td><td>{c['cust']}</td><td>{c['exec_']}</td>"
            f"<td><div style='background:linear-gradient(90deg,#3498DB {bar_w:.0f}%,#ECF0F1 {bar_w:.0f}%);height:14px;border-radius:3px'></div></td></tr>"
        )

    # Category table with side split
    cat_rows = []
    for cat, tot in top_cats[:15]:
        cu = cat_side.get((cat, "customer"), 0)
        ex = cat_side.get((cat, "executor"), 0)
        share = tot / summary["total"] * 100 if summary["total"] else 0
        cat_rows.append(
            f"<tr><td>{cat}</td><td>{tot}</td><td>{cu}</td><td>{ex}</td><td>{share:.1f}%</td></tr>"
        )

    donut = _side_donut(summary["customer"], summary["executor"])

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Май 2026 — анализ (v3)</title>
<style>{CSS_COMMON}
.row-flex {{ display:flex; gap:16px; align-items:center; }}
.row-flex > div:first-child {{ flex-shrink:0; }}
table.top15 th.rot {{ writing-mode: vertical-rl; transform: rotate(180deg); padding:8px 4px; font-size:10px; height:140px; vertical-align:bottom; white-space:nowrap; max-width:32px; }}
table.cc-heatmap td.cn {{ max-width:280px; font-size:10.5px; }}
table.cc-heatmap th {{ font-size:10.5px; }}
table.cc-heatmap tfoot td {{ background:#34495E; color:#fff; font-weight:700; }}
table.cc-heatmap tfoot td.cn {{ background:#2C3E50; }}
</style></head><body>

<div class="hero" style="background:linear-gradient(135deg,#E74C3C,#922B21)">
  <h1>Май 2026 — детальный анализ</h1>
  <div class="sub">Детальный отчёт на базе ChatApp / Bitrix / Flomni</div>
</div>

<div class="kpis">
  <div class="kpi"><div class="v">{summary["total"]}</div><div class="l">Всего тикетов</div></div>
  <div class="kpi"><div class="v">{summary["customer"]}</div><div class="l">От заказчиков</div></div>
  <div class="kpi"><div class="v">{summary["executor"]}</div><div class="l">От исполнителей</div></div>
  <div class="kpi"><div class="v">{summary["chatapp"]+summary["bitrix"]+summary["flomni"]}</div><div class="l">По источникам</div></div>
</div>

<h2>1. Источники и распределение по сторонам</h2>
<div class="card">
<div class="row-flex">
  <div>{donut}</div>
  <div style="flex:1">
    <table>
      <thead><tr><th>Источник</th><th>Тикетов</th><th>% от мая</th></tr></thead>
      <tbody>
        <tr><td>ChatApp (Telegram)</td><td>{summary["chatapp"]}</td><td>{summary["chatapp"]/summary["total"]*100:.1f}%</td></tr>
        <tr><td>Bitrix</td><td>{summary["bitrix"]}</td><td>{summary["bitrix"]/summary["total"]*100:.1f}%</td></tr>
        <tr><td>Flomni</td><td>{summary["flomni"]}</td><td>{summary["flomni"]/summary["total"]*100:.1f}%</td></tr>
      </tbody>
    </table>
  </div>
</div>
<div class="legend">
  <span><span class="sw" style="background:#E74C3C"></span>От заказчика ({summary["customer"]/summary["total"]*100:.0f}%)</span>
  <span><span class="sw" style="background:#27AE60"></span>От исполнителя ({summary["executor"]/summary["total"]*100:.0f}%)</span>
</div>
</div>

<h2>2. Топ-10 компаний мая</h2>
<div class="card">
<table>
<thead><tr><th>#</th><th>Компания</th><th>Всего</th><th>Cust</th><th>Exec</th><th>Объём</th></tr></thead>
<tbody>{"".join(top_rows)}</tbody>
</table>
</div>

<h2>3. Категории обращений (обе стороны)</h2>
<div class="card">
<table>
<thead><tr><th>Категория</th><th>Всего</th><th>От заказчиков</th><th>От исполнителей</th><th>% мая</th></tr></thead>
<tbody>{"".join(cat_rows)}</tbody>
</table>
</div>

<h2>4. Heatmap: Топ-10 категорий × Топ-10 компаний (май)</h2>
<div class="card">{comp_cat_heatmap}</div>

<h2>5. Детализация по топ-5 компаниям мая</h2>
<p style="color:#566573;font-size:12px;margin-bottom:8px">
Только данные мая — динамика по неделям не показана, акцент на категории.</p>
{deep_dive}

<div class="disclaimer">
Методология подсчёта: 1 тикет = 1 календарный день на чат (ChatApp);
выходные (Пт ≥18:00 / Сб / Вс) объединяются в одно 72-часовое окно.
</div>

</body></html>"""

    path = REPORTS_DIR / "may_focus_v3.html"
    path.write_text(html, encoding="utf-8")
    log.info("HTML written: %s", path)
    return path


# =====================================================================
# PDF + main
# =====================================================================

# =====================================================================
# REPORT 3 v3 — May XLS template (with ChatApp / Flomni split)
# =====================================================================

MAY_WEEKS = [
    ("1.05-3.05",   "2026-05-01", "2026-05-03"),
    ("4.05-10.05",  "2026-05-04", "2026-05-10"),
    ("11.05-17.05", "2026-05-11", "2026-05-17"),
    ("18.05-24.05", "2026-05-18", "2026-05-24"),
    ("25.05-31.05", "2026-05-25", "2026-05-31"),
]

CATEGORIES_EXEC = [
    "Другое", "Выплаты и проблемы с ними", "KYC", "ИП РФ", "SEPA/SWIFT",
    "Изменение/удаление аккаунта", "Курсы/комиссии/лимиты",
    "Функциональность сервиса и возможности выплат", "Техническая проблема/вопрос",
    "Статус ИП РФ/Самозанятого", "Запрос документов", "Потенциальный клиент",
    "НДФЛ", "Тест", "Реквизиты заблокированы",
    "Предложение (маркетинг, сотрудничество, банкинг)", "EOR",
    "Вопросы по работе в сервисе",
]

CATEGORIES_CUST = [
    "Проблема KYC", "Дублирование KYC", "Выплаты и проблемы с ними",
    "Техническая проблема/вопрос", "Запрос документов не бух",
    "Зачисление платежа на баланс",
    "Вопросы по числам отправки закрывашек в эдо заказчику/поторопить бухгалтерию",
    "Вопросы по работе в сервисе", "Функциональность сервиса и возможность выплат",
    "Изменения в профиле заказчика/исполнителя", "Налоговый статус исполнителя",
    "Другое",
]


def _fetch_xls_data(source_filter: str | None) -> dict:
    """Return {(side, category, week_label): count} for May."""
    source_clause = f"AND source = '{source_filter}'" if source_filter else ""
    rows = _fetch(f"""
        SELECT may_side, may_category, dialog_date, COUNT(*) AS n
          FROM analytics_archive_jan_may
         WHERE to_char(dialog_date,'YYYY-MM') = '2026-05'
           AND company IS DISTINCT FROM 'Systems'
           AND may_side IN ('customer','executor')
           AND may_category IS NOT NULL
           {source_clause}
         GROUP BY 1,2,3
    """)
    by_key: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in rows:
        d = r["dialog_date"].isoformat()
        wl = None
        for label, a, b in MAY_WEEKS:
            if a <= d <= b:
                wl = label
                break
        if wl is None:
            continue
        by_key[(r["may_side"], r["may_category"], wl)] += r["n"]
    return by_key


def _write_xls_table(ws, start_row: int, title: str, data: dict) -> int:
    """Render one table (header + executor section + customer section) starting at start_row.
    Returns next free row after the table."""
    from openpyxl.styles import Alignment, Font, PatternFill

    bold = Font(bold=True)
    title_font = Font(bold=True, size=13, color="1A2935")
    hdr_fill = PatternFill("solid", fgColor="34495E")
    hdr_font = Font(bold=True, color="FFFFFF")
    section_fill = PatternFill("solid", fgColor="D5DBDB")

    week_labels = [w[0] for w in MAY_WEEKS]
    n_cols = len(week_labels) + 2  # name + weeks + total

    # Title row
    ws.cell(row=start_row, column=1, value=title).font = title_font
    r = start_row + 1
    # Header
    ws.cell(row=r, column=1, value="Категории")
    for i, wl in enumerate(week_labels):
        ws.cell(row=r, column=2 + i, value=wl)
    ws.cell(row=r, column=n_cols, value="Всего")
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=r, column=c)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center")
    r += 1

    def section(side_label: str, side_key: str, cats: list[str]) -> None:
        nonlocal r
        # Section header row (filled after we know totals)
        section_row = r
        ws.cell(row=r, column=1, value=f"Запросы от {side_label}:")
        r += 1
        week_totals = [0] * len(MAY_WEEKS)
        grand_total = 0
        for cat in cats:
            counts = [data.get((side_key, cat, w), 0) for w in week_labels]
            row_tot = sum(counts)
            for i, c in enumerate(counts):
                week_totals[i] += c
            grand_total += row_tot
            ws.cell(row=r, column=1, value=cat)
            for i, c in enumerate(counts):
                if c:
                    ws.cell(row=r, column=2 + i, value=c)
            if row_tot:
                ws.cell(row=r, column=n_cols, value=row_tot)
            r += 1
        # Backfill section header with totals
        for i, t in enumerate(week_totals):
            ws.cell(row=section_row, column=2 + i, value=t)
        ws.cell(row=section_row, column=n_cols, value=grand_total)
        for c in range(1, n_cols + 1):
            cell = ws.cell(row=section_row, column=c)
            cell.fill = section_fill
            cell.font = bold

    section("исполнителей", "executor", CATEGORIES_EXEC)
    section("заказчиков", "customer", CATEGORIES_CUST)
    return r + 2  # 2-row gap before next table


def build_xls_v3() -> Path:
    log.info("Building may_template_v3.xlsx")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Май 2026"

    r = 1
    r = _write_xls_table(ws, r, "Май 2026 — все источники", _fetch_xls_data(None))
    r = _write_xls_table(ws, r, "Май 2026 — ChatApp", _fetch_xls_data("chatapp"))
    r = _write_xls_table(ws, r, "Май 2026 — Flomni", _fetch_xls_data("flomni"))

    # Column widths
    ws.column_dimensions["A"].width = 55
    for col_letter in ("B", "C", "D", "E", "F", "G"):
        ws.column_dimensions[col_letter].width = 13

    out = REPORTS_DIR / "may_template_v3.xlsx"
    wb.save(out)
    log.info("XLS written: %s", out)
    return out


def _html_to_pdf(html_path: Path) -> Path:
    pdf_path = html_path.with_suffix(".pdf")
    cmd = [
        "google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", f"file://{html_path}",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        log.info("PDF written: %s", pdf_path)
    except Exception as e:
        log.warning("PDF generation failed for %s: %s", html_path, e)
    return pdf_path


def main() -> None:
    p1 = build_overview_v3()
    p2 = build_may_v3()
    build_xls_v3()
    _html_to_pdf(p1)
    _html_to_pdf(p2)
    log.info("DONE. Outputs in %s", REPORTS_DIR)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    main()
