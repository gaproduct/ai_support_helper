"""
Ad-hoc отчёт по августу 2026, методология E, из live-БД
(таблица tickets + последний AnalysisResult для подкатегорий). Печатает HTML в stdout.

Границы периода задаются в DATE_FROM/DATE_TO и PERIOD, больше нигде не зашиты.

Запуск (из контейнера, читает БД, пишет HTML на хост):
  docker exec -i support_tickets-webhook-1 python - < build_august_report.py > out.html
"""
from collections import defaultdict

from sqlalchemy import text
from database import engine
import fine_subcategory as fs
import repeat_contacts as rc

DATE_FROM, DATE_TO = "2026-08-01", "2026-08-27"
PERIOD = "1–27 августа 2026"
PERIOD_SHORT = "1–27"

# Теневые дубли: тот же чат Telegram, пришедший вторым каналом через Flomni.
# Без этого фильтра часть обращений считается дважды. Витрины Superset его
# ставят, отчёт должен считать так же, иначе цифры не сойдутся.
NO_SHADOW = ("AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs sd "
             "WHERE sd.dialog_id = t.dialog_id)")
NO_SHADOW_BARE = ("AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs sd "
                  "WHERE sd.dialog_id = tickets.dialog_id)")

SUBCAT_CATEGORIES = (
    "Выплаты и проблемы с ними",
    "KYC",
    "Запрос документов",
    "Вопросы по работе в сервисе",
    "Техническая проблема/вопрос",
)
WEEKS = [
    ("01-07 августа", "2026-08-01", "2026-08-07"),
    ("08-14 августа", "2026-08-08", "2026-08-14"),
    ("15-21 августа", "2026-08-15", "2026-08-21"),
    ("22-27 августа", "2026-08-22", "2026-08-27"),
]


def q(sql, **p):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings().all()]


P = {"a": DATE_FROM, "b": DATE_TO,
     "depcat": fs.DEPOSIT_CATEGORY, "depbucket": fs.DEPOSIT_BUCKET,
     "kycmerge": fs.KYC_MERGED_SUB}

# Диалоги бакета «Пополнение/депозит» переносятся в отдельную категорию
# (это зачисление на баланс платформы заказчиком, а не выплата исполнителю).
# Плюс ручные корректировки AI-мисклассификации (manual_category_override).
DEP_JOIN = ("LEFT JOIN (SELECT dialog_id, :depcat eff FROM dialog_fine_subcategory "
            "WHERE fine_bucket=:depbucket) dep ON dep.dialog_id=t.dialog_id "
            "LEFT JOIN manual_category_override ovr ON ovr.dialog_id=t.dialog_id")
EFFCAT = "COALESCE(ovr.category, dep.eff, t.category)"

# Ручное назначение подкатегорий диалогам без AI-подкатегории (разобраны по одиночке,
# таблица manual_subcategory_override) + корректировки мисклассификации (ovr).
TECH_JOIN = "LEFT JOIN manual_subcategory_override mso ON mso.dialog_id=t.dialog_id"
_RAWSUB = "COALESCE(ovr.subcategory, NULLIF(ar.subcategory,''), mso.subcategory)"
EFFSUB = (f"CASE {_RAWSUB} WHEN 'Запрос на верификацию' THEN :kycmerge "
          f"WHEN 'Ошибка прохождения' THEN :kycmerge ELSE {_RAWSUB} END")

summary = q(f"""
    SELECT COALESCE(SUM(count),0) total,
           COALESCE(SUM(count) FILTER (WHERE side='customer'),0) customer,
           COALESCE(SUM(count) FILTER (WHERE side='executor'),0) executor,
           COUNT(DISTINCT dialog_id) dialogs
      FROM tickets WHERE methodology='E' AND dialog_date BETWEEN :a AND :b
       {NO_SHADOW_BARE}
""", **P)[0]

by_source = q(f"""
    SELECT source, SUM(count) n FROM tickets
     WHERE methodology='E' AND dialog_date BETWEEN :a AND :b
       {NO_SHADOW_BARE}
     GROUP BY source ORDER BY n DESC
""", **P)

cats = q(f"""
    SELECT {EFFCAT} category, SUM(t.count) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_
      FROM tickets t {DEP_JOIN}
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       {NO_SHADOW}
     GROUP BY {EFFCAT} ORDER BY total DESC
""", **P)

companies = q(f"""
    SELECT company, SUM(count) total,
           COALESCE(SUM(count) FILTER (WHERE side='customer'),0) cust,
           COALESCE(SUM(count) FILTER (WHERE side='executor'),0) exec_
      FROM tickets WHERE methodology='E' AND dialog_date BETWEEN :a AND :b
       AND company IS NOT NULL AND company<>'' AND company<>'Unknown'
       {NO_SHADOW_BARE}
     GROUP BY company ORDER BY total DESC LIMIT 15
""", **P)

# Subcategory detail: join latest AR for subcategory (deposit dialogs excluded —
# they get category «Пополнение...» via override, which is not in SUBCAT_CATEGORIES)
subcats = q(f"""
    SELECT {EFFCAT} category, {EFFSUB} subcategory,
           SUM(t.count) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_
      FROM tickets t
      JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
           ON l.dialog_id=t.dialog_id
      JOIN analysis_results ar ON ar.id=l.mx
      {DEP_JOIN}
      {TECH_JOIN}
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND {EFFCAT} = ANY(:cats)
       {NO_SHADOW}
     GROUP BY {EFFCAT}, {EFFSUB}
""", cats=list(SUBCAT_CATEGORIES), **P)

# Fine-buckets (dialog_fine_subcategory). Deposit bucket excluded — вынесен в
# отдельную категорию, поэтому под «Зависанием» его больше нет.
fine = q(f"""
    SELECT f.subcategory, f.fine_bucket,
           SUM(t.count) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_,
           COUNT(DISTINCT t.dialog_id) dialogs
      FROM tickets t
      JOIN dialog_fine_subcategory f ON f.dialog_id=t.dialog_id
      LEFT JOIN manual_category_override ovr ON ovr.dialog_id=t.dialog_id
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND f.fine_bucket <> :depbucket
       -- диалоги с ручной правкой категории берём только если бакет посчитан
       -- под ту же подкатегорию, иначе он остался от старой классификации
       AND (ovr.dialog_id IS NULL OR ovr.subcategory = f.subcategory)
       {NO_SHADOW}
     GROUP BY f.subcategory, f.fine_bucket
""", **P)

# Deposit category detail (carved-out «Пополнение и зачисление на баланс»)
dep_summary = q(f"""
    SELECT COALESCE(SUM(t.count),0) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_,
           COUNT(DISTINCT t.dialog_id) dialogs
      FROM tickets t
      JOIN dialog_fine_subcategory f ON f.dialog_id=t.dialog_id
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND f.fine_bucket = :depbucket
       {NO_SHADOW}
""", **P)[0]
# Cross-cutting KYC slice: bucket «Требуется верификация/KYC» aggregated across
# BOTH payment subcategories (отказ + зависание) — сквозной срез.
KYC_BUCKET = "Требуется верификация/KYC"
kyc_cross = q(f"""
    SELECT COALESCE(SUM(t.count),0) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_,
           COALESCE(SUM(t.count) FILTER (WHERE f.subcategory='Уточнение причин отказа по платежу'),0) refusal,
           COALESCE(SUM(t.count) FILTER (WHERE f.subcategory='Зависание в обработке'),0) hangup,
           COUNT(DISTINCT t.dialog_id) dialogs
      FROM tickets t
      JOIN dialog_fine_subcategory f ON f.dialog_id=t.dialog_id
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND f.fine_bucket = :kb
       {NO_SHADOW}
""", kb=KYC_BUCKET, **P)[0]
# Payments (Выплаты) subcategory × company heatmap (point 2)
PAY_CAT = "Выплаты и проблемы с ними"
pay_sub_rows = q(f"""
    SELECT t.company, ar.subcategory, SUM(t.count) n
      FROM tickets t
      JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
           ON l.dialog_id=t.dialog_id
      JOIN analysis_results ar ON ar.id=l.mx
      {DEP_JOIN}
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND {EFFCAT} = :paycat
       AND t.company IS NOT NULL AND t.company<>'' AND t.company<>'Unknown'
       {NO_SHADOW}
     GROUP BY t.company, ar.subcategory
""", paycat=PAY_CAT, **P)

# Weeks
weeks_data = []
for label, a, b in WEEKS:
    r = q(f"""
        SELECT COALESCE(SUM(count),0) tickets, COUNT(DISTINCT dialog_id) dialogs
          FROM tickets WHERE methodology='E' AND dialog_date BETWEEN :a AND :b
           {NO_SHADOW_BARE}
    """, a=a, b=b)[0]
    weeks_data.append((label, a, b, r["dialogs"], r["tickets"]))

# Heatmap cats × companies (top10 each)
top10_comp = [c["company"] for c in companies[:10]]
top10_cat = [c["category"] for c in cats[:10]]
cc_rows = q(f"""
    SELECT t.company company, {EFFCAT} category, SUM(t.count) n FROM tickets t {DEP_JOIN}
     WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
       AND t.company = ANY(:cs) AND {EFFCAT} = ANY(:ks)
       {NO_SHADOW}
     GROUP BY t.company, {EFFCAT}
""", cs=top10_comp, ks=top10_cat, **P)
cc = {(r["company"], r["category"]): r["n"] for r in cc_rows}

# Deep dive top-15 companies: category + subcategory
deep = []
for c in companies[:15]:
    rows = q(f"""
        SELECT {EFFCAT} category, {EFFSUB} subcategory,
               SUM(t.count) total,
               COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
               COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_
          FROM tickets t
          JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
               ON l.dialog_id=t.dialog_id
          JOIN analysis_results ar ON ar.id=l.mx
          {DEP_JOIN}
          {TECH_JOIN}
         WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
           AND t.company=:co
           {NO_SHADOW}
         GROUP BY {EFFCAT}, {EFFSUB}
    """, co=c["company"], **P)
    deep.append((c, rows))

# Per-ticket deep dive for named companies (one row per dialog with problem summary).
# Каждый пункт: (отображаемое имя, [варианты названия в БД]).
DETAIL_COMPANIES = [(c["company"], [c["company"]]) for c in companies[:5]]
company_ticket_detail = {}
for disp, variants in DETAIL_COMPANIES:
    rows = q(f"""
        SELECT t.dialog_id, t.side, t.count, t.dialog_date::date dd,
               {EFFCAT} category, {EFFSUB} subcategory,
               f.fine_bucket, ar.summary
          FROM tickets t
          JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
               ON l.dialog_id=t.dialog_id
          JOIN analysis_results ar ON ar.id=l.mx
          {DEP_JOIN}
          {TECH_JOIN}
          LEFT JOIN dialog_fine_subcategory f ON f.dialog_id=t.dialog_id
         WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
           AND t.company = ANY(:cos)
           {NO_SHADOW}
         ORDER BY {EFFCAT}, t.count DESC, t.dialog_id
    """, cos=variants, **P)
    company_ticket_detail[disp] = rows

# Potential clients: standalone list (one row per ticket)
potential = q(f"""
    SELECT t.dialog_id, t.source, t.side, t.dialog_date::date dd,
           COALESCE(NULLIF(t.company,''),'—') company,
           d.client_id, d.license_id, d.messenger_type, d.chat_name,
           ar.summary
      FROM tickets t
      JOIN dialogs d ON d.id=t.dialog_id
      JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
           ON l.dialog_id=t.dialog_id
      JOIN analysis_results ar ON ar.id=l.mx
     WHERE t.methodology='E' AND t.category='Потенциальный клиент'
       AND t.dialog_date BETWEEN :a AND :b
       {NO_SHADOW}
     ORDER BY t.dialog_date, t.dialog_id
""", **P)

# ---------- render ----------
total = summary["total"]


def pct(n):
    return f"{n/total*100:.1f}%" if total else "0%"


def heat(intensity):
    intensity = max(0.0, min(1.0, intensity))
    r = int(236 - (236 - 41) * intensity)
    g = int(240 - (240 - 128) * intensity)
    b = int(241 - (241 - 185) * intensity)
    return f"rgb({r},{g},{b})"


CSS = """@page{size:A4;margin:18mm 12mm}body{font-family:-apple-system,"Segoe UI",Arial,sans-serif;color:#2C3E50;line-height:1.4}h1{color:#1A2935;font-size:26px;margin:0 0 4px}h2{color:#34495E;font-size:18px;border-left:4px solid #3498DB;padding-left:10px;margin:24px 0 12px;page-break-after:avoid}h3{color:#2C3E50;font-size:16px;margin:18px 0 8px}h4{color:#34495E;font-size:13px;margin:10px 0 6px}.hero{background:linear-gradient(135deg,#E74C3C,#922B21);color:#fff;padding:18px 22px;border-radius:8px;margin-bottom:18px}.hero .sub{opacity:.85;font-size:13px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}.kpi{background:#fff;border:1px solid #E6EAEE;border-radius:6px;padding:10px 12px;text-align:center}.kpi .v{font-size:24px;font-weight:700;color:#3498DB}.kpi .l{font-size:10px;text-transform:uppercase;color:#7f8c8d;margin-top:2px}.card{border:1px solid #E6EAEE;border-radius:6px;padding:14px 16px;margin-bottom:14px;background:#fff}table{width:100%;border-collapse:collapse;font-size:11px}th{background:#34495E;color:#fff;padding:6px 8px;text-align:left;font-weight:600}td{padding:5px 8px;border-bottom:1px solid #ECF0F1}table.top15 td.rank{text-align:center;font-weight:700;color:#7f8c8d;width:30px}table.top15 td.cn{font-weight:600;max-width:220px}table.top15 td.tot{font-weight:700;background:#34495E;color:#fff;text-align:center}table.top15 td{text-align:center}.company-card{background:#FBFCFD;border:1px solid #DCE3EA;border-radius:8px;padding:14px 18px;margin-bottom:18px}.company-card h3{font-size:18px;margin-top:0;color:#1A2935}.stats{display:flex;gap:16px;margin:12px 0}.stat{flex:1;text-align:center;background:#fff;border:1px solid #E6EAEE;border-radius:6px;padding:10px}.stat .val{font-size:24px;font-weight:700;color:#3498DB}.stat .lbl{font-size:10px;color:#7f8c8d;text-transform:uppercase;margin-top:2px}table.d{font-size:10px}.subcat td.sc{padding-left:24px;color:#566573;font-style:italic}.subcat td.sc::before{content:"\\21B3 ";color:#95A5A6}.fine td{background:#FCFDFE;font-size:10px}.fine td.fc{padding-left:48px;color:#7F8C8D}.fine td.fc::before{content:"\\2022 ";color:#BDC3C7}.fine .fd{color:#AAB2BB;font-style:normal}.disclaimer{font-size:9px;color:#95A5A6;font-style:italic;margin-top:14px}.legend{display:flex;gap:14px;font-size:11px;margin:8px 0 0;color:#7F8C8D}.legend .sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px}.row-flex{display:flex;gap:16px;align-items:center}.row-flex>div:first-child{flex-shrink:0}table.top15 th.rot{writing-mode:vertical-rl;transform:rotate(180deg);padding:8px 4px;font-size:10px;height:140px;vertical-align:bottom;white-space:nowrap;max-width:32px}table.cc-heatmap td.cn{max-width:280px;font-size:10.5px}table.cc-heatmap th{font-size:10.5px}table.cc-heatmap tfoot td{background:#34495E;color:#fff;font-weight:700}table.cc-heatmap tfoot td.cn{background:#2C3E50}.week-bars{display:flex;gap:8px;align-items:flex-end;height:120px;margin:12px 0 4px}.week-bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}.week-bar{width:100%;background:#3498DB;border-radius:4px 4px 0 0;display:flex;align-items:flex-start;justify-content:center;padding-top:4px;color:#fff;font-size:10px;font-weight:700}.week-label{font-size:9px;color:#7f8c8d;text-align:center}.subcat-block{background:#F8F9FA;border-left:3px solid #3498DB;padding:8px 14px;margin:10px 0}.subcat-block h4{margin:0 0 6px;color:#1A2935}table.pc td{text-align:left;vertical-align:top}table.pc td.rank{text-align:center}table.pc th{text-align:left}.brandbar{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px}.brandbar .logo{background:#fff;color:#E74C3C;padding:2px 8px;border-radius:4px;font-weight:800;letter-spacing:.5px}.brandbar .sep{opacity:.55}.footer{margin-top:28px;padding-top:12px;border-top:2px solid #E74C3C;font-size:10px;color:#7F8C8D;display:flex;justify-content:space-between;align-items:center}.footer b{color:#E74C3C}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}@media print{thead{display:table-header-group}tr,.kpi,.stat,.subcat-block,.company-card,.card,.week-bars{page-break-inside:avoid}h1,h2,h3,h4{page-break-after:avoid}.hero{page-break-after:avoid}}"""

out = []
out.append(f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Август 2026 ({PERIOD_SHORT})</title><style>' + CSS + '</style></head><body>')
out.append(f'<div class="hero"><div class="brandbar"><span class="logo">MadeTask</span><span class="sep">×</span><span>Remozo</span><span class="sep">·</span><span>Аналитика поддержки</span></div><h1>Август 2026 ({PERIOD_SHORT}) — детальный анализ</h1><div class="sub">Период: 01.08.2026 – 27.08.2026 · Источники: ChatApp + Flomni · Методология E (несколько ID в одном сообщении = 1 тикет) · с детализацией по подкатегориям</div></div>')
out.append(f'<div class="kpis"><div class="kpi"><div class="v">{total}</div><div class="l">Всего тикетов</div></div><div class="kpi"><div class="v">{summary["customer"]}</div><div class="l">От заказчиков</div></div><div class="kpi"><div class="v">{summary["executor"]}</div><div class="l">От исполнителей</div></div><div class="kpi"><div class="v">{summary["dialogs"]}</div><div class="l">Диалогов</div></div></div>')

# 1. Sources & side
cust_share = summary["customer"] / total * 100 if total else 0
dash = cust_share * 2.512
src_rows = "".join(f'<tr><td>{"ChatApp (Telegram)" if r["source"]=="chatapp" else r["source"].capitalize()}</td><td>{r["n"]}</td><td>{pct(r["n"])}</td></tr>' for r in by_source)
out.append('<h2>1. Источники и распределение по сторонам</h2>')
out.append(f'<div class="card"><div class="row-flex"><div><svg viewBox="0 0 100 100" width="140" height="140"><circle cx="50" cy="50" r="40" fill="none" stroke="#27AE60" stroke-width="14"/><circle cx="50" cy="50" r="40" fill="none" stroke="#E74C3C" stroke-width="14" stroke-dasharray="{dash:.1f} 251.2" transform="rotate(-90 50 50)"/><text x="50" y="48" text-anchor="middle" font-size="14" font-weight="700" fill="#2C3E50">{cust_share:.0f}%</text><text x="50" y="62" text-anchor="middle" font-size="6" fill="#7F8C8D">заказчики</text></svg></div><div style="flex:1"><table><thead><tr><th>Источник</th><th>Тикетов</th><th>% от периода</th></tr></thead><tbody>{src_rows}</tbody></table></div></div><div class="legend"><span><span class="sw" style="background:#E74C3C"></span>От заказчика ({cust_share:.0f}%)</span><span><span class="sw" style="background:#27AE60"></span>От исполнителя ({100-cust_share:.0f}%)</span></div></div>')

# 2. Weeks
max_wt = max((w[4] for w in weeks_data), default=1) or 1
bars = "".join(f'<div class="week-bar-wrap"><div class="week-bar" style="height:{w[4]/max_wt*100:.0f}%">{w[4]}</div><div class="week-label">{w[0]}</div></div>' for w in weeks_data)
wrows = "".join(f'<tr><td>{w[0]}</td><td>{w[3]}</td><td>{w[4]}</td></tr>' for w in weeks_data)
out.append('<h2>2. Динамика по неделям</h2>')
out.append(f'<div class="card"><div class="week-bars">{bars}</div><table style="margin-top:8px"><thead><tr><th>Неделя</th><th>Диалогов</th><th>Тикетов</th></tr></thead><tbody>{wrows}</tbody></table></div>')

# 3. Companies
max_c = max((c["total"] for c in companies), default=1) or 1
crows = "".join(f'<tr><td>{i+1}</td><td>{c["company"]}</td><td>{c["total"]}</td><td>{c["cust"]}</td><td>{c["exec_"]}</td><td><div style="background:linear-gradient(90deg,#3498DB {c["total"]/max_c*100:.0f}%,#ECF0F1 {c["total"]/max_c*100:.0f}%);height:14px;border-radius:3px"></div></td></tr>' for i, c in enumerate(companies))
out.append(f'<h2>3. Топ-{len(companies)} компаний ({PERIOD})</h2>')
out.append(f'<div class="card"><table><thead><tr><th>#</th><th>Компания</th><th>Всего</th><th>Cust</th><th>Exec</th><th>Объём</th></tr></thead><tbody>{crows}</tbody></table></div>')

# 4. Categories
catrows = "".join(f'<tr><td>{c["category"]}</td><td>{c["total"]}</td><td>{c["cust"]}</td><td>{c["exec_"]}</td><td>{pct(c["total"])}</td></tr>' for c in cats)
out.append('<h2>4. Категории обращений (обе стороны)</h2>')
out.append(f'<div class="card"><table><thead><tr><th>Категория</th><th>Всего</th><th>От заказчиков</th><th>От исполнителей</th><th>% периода</th></tr></thead><tbody>{catrows}</tbody></table></div>')

# 5. Subcategory detail
sub_by_cat = defaultdict(list)
for r in subcats:
    sub_by_cat[r["category"]].append(r)
cat_total_map = {c["category"]: c["total"] for c in cats}
# fine-buckets indexed by parent subcategory, ordered canonically (fs.FINE_ORDER)
fine_by_sub = defaultdict(dict)
for r in fine:
    fine_by_sub[r["subcategory"]][r["fine_bucket"]] = r
out.append('<h2>5. Детализация по подкатегориям</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">Разбивка тикетов внутри категорий, у которых заданы подкатегории. Категории, подкатегории и уточняющие бакеты идут по убыванию числа тикетов, показаны топ-7 самых весомых категорий.</p>')
# Порядок блоков: по убыванию числа тикетов, топ-7.
TOP_SUBCAT_BLOCKS = 7
_ordered = sorted(
    sub_by_cat.items(),
    key=lambda cr: -cat_total_map.get(cr[0], sum(r["total"] for r in cr[1])),
)
DISPLAY_ORDER = [c for c, _ in _ordered[:TOP_SUBCAT_BLOCKS]]
for cat in DISPLAY_ORDER:
    rows = sub_by_cat.get(cat)
    if not rows:
        continue
    rows = sorted(rows, key=lambda r: -r["total"])
    ctot = cat_total_map.get(cat, sum(r["total"] for r in rows))
    body = ""
    for r in rows:
        name = r["subcategory"] or "(без подкатегории)"
        sh = r["total"]/ctot*100 if ctot else 0
        body += f'<tr class="subcat"><td class="sc">{name}</td><td>{r["total"]}</td><td>{r["cust"]}</td><td>{r["exec_"]}</td><td>{sh:.0f}%</td></tr>'
        # nested fine-buckets for the two segmented payment subcategories
        fmap = fine_by_sub.get(name)
        if fmap:
            stot = r["total"]
            # Бакеты внутри подкатегории — тоже по убыванию числа тикетов.
            for bucket, fr in sorted(fmap.items(), key=lambda kv: -kv[1]["total"]):
                fsh = fr["total"]/stot*100 if stot else 0
                body += (f'<tr class="fine"><td class="fc">{bucket}</td>'
                         f'<td>{fr["total"]}</td><td>{fr["cust"]}</td>'
                         f'<td>{fr["exec_"]}</td><td>{fsh:.0f}%</td></tr>')
    out.append(f'<div class="subcat-block"><h4>{cat} · {ctot} тикетов</h4><table class="d"><thead><tr><th>Подкатегория</th><th>Всего</th><th>Cust</th><th>Exec</th><th>% категории</th></tr></thead><tbody>{body}</tbody></table>')
    # Cross-cutting KYC callout under the payments category
    if cat == PAY_CAT and kyc_cross["total"]:
        ksh = kyc_cross["total"]/ctot*100 if ctot else 0
        out.append(
            f'<div style="margin-top:8px;padding:10px 12px;background:#eef5fb;border-left:3px solid #2e86c1;border-radius:4px;font-size:12px;color:#2c3e50">'
            f'<b>Сквозной срез: связано с верификацией/KYC</b> — <b>{kyc_cross["total"]}</b> тикетов '
            f'({ksh:.0f}% категории), из них {kyc_cross["cust"]} от заказчиков / {kyc_cross["exec_"]} от исполнителей.<br>'
            f'Bucket «{KYC_BUCKET}» объединён из двух подкатегорий: '
            f'«Уточнение причин отказа» (выплата <b>заблокирована</b> из-за KYC) — {kyc_cross["refusal"]} '
            f'и «Зависание в обработке» (выплата <b>ждёт</b> прохождения/проверки KYC) — {kyc_cross["hangup"]}.'
            f'</div>')
    out.append('</div>')

# Deposit category rendered inside section 5 as a standalone category (no sub-detail)
if dep_summary["total"]:
    dtot = dep_summary["total"]
    out.append(f'<div class="subcat-block"><h4>Пополнение и зачисление на баланс · {dtot} тикетов</h4>'
               f'<p style="color:#566573;font-size:11px;margin:0 0 6px">Вынесено из «Выплат и проблем с ними»: зачисление средств на баланс платформы <b>заказчиком</b> (USDT / банковские переводы, контроль сроков), а не выплата исполнителю. Отдельная категория без детализации по подкатегориям.</p>'
               f'<table class="d"><thead><tr><th>Категория</th><th>Всего</th><th>Cust</th><th>Exec</th><th>Диалогов</th></tr></thead>'
               f'<tbody><tr class="subcat"><td class="sc">Пополнение/зачисление на баланс (заказчик)</td><td>{dtot}</td><td>{dep_summary["cust"]}</td><td>{dep_summary["exec_"]}</td><td>{dep_summary["dialogs"]}</td></tr></tbody></table></div>')

# 7. Heatmap
max_v = max((cc.get((co, ca), 0) for co in top10_comp for ca in top10_cat), default=1) or 1
heads = "".join(f'<th class="rot">{co}</th>' for co in top10_comp)
hrows = []
for i, ca in enumerate(top10_cat):
    cells = ""
    rowtot = 0
    for co in top10_comp:
        n = cc.get((co, ca), 0)
        rowtot += n
        inten = n/max_v
        cells += f'<td style="background:{heat(inten)};color:{"#fff" if inten>0.5 else "#2c3e50"}">{n or ""}</td>'
    hrows.append(f'<tr><td class="rank">{i+1}</td><td class="cn">{ca}</td>{cells}<td class="tot">{rowtot}</td></tr>')
comp_tot = {c["company"]: c["total"] for c in companies}
foot = "".join(f'<td class="tot">{comp_tot.get(co,0)}</td>' for co in top10_comp)
out.append('<h2>6. Heatmap: Топ-10 категорий × Топ-10 компаний</h2>')
out.append(f'<div class="card"><table class="top15 cc-heatmap"><thead><tr><th>#</th><th>Категория</th>{heads}<th>Всего</th></tr></thead><tbody>{"".join(hrows)}</tbody><tfoot><tr><td></td><td class="cn">Всего по компании</td>{foot}<td class="tot">{sum(comp_tot.get(co,0) for co in top10_comp)}</td></tr></tfoot></table></div>')

# 8. Heatmap: Выплаты — подкатегории × компании (point 2)
pay_cc = {(r["company"], r["subcategory"]): r["n"] for r in pay_sub_rows}
pay_comp_tot = defaultdict(int)
pay_sub_tot = defaultdict(int)
for r in pay_sub_rows:
    pay_comp_tot[r["company"]] += r["n"]
    pay_sub_tot[r["subcategory"] or "—"] += r["n"]
top_pcomp = [c for c, _ in sorted(pay_comp_tot.items(), key=lambda x: -x[1])[:10]]
top_psub = [s for s, _ in sorted(pay_sub_tot.items(), key=lambda x: -x[1])[:10]]
pmax = max((pay_cc.get((co, s), 0) for co in top_pcomp for s in top_psub), default=1) or 1
pheads = "".join(f'<th class="rot">{co}</th>' for co in top_pcomp)
prows_h = []
for i, s in enumerate(top_psub):
    cells = ""
    rowtot = 0
    for co in top_pcomp:
        n = pay_cc.get((co, s), 0)
        rowtot += n
        inten = n/pmax
        cells += f'<td style="background:{heat(inten)};color:{"#fff" if inten>0.5 else "#2c3e50"}">{n or ""}</td>'
    prows_h.append(f'<tr><td class="rank">{i+1}</td><td class="cn">{s}</td>{cells}<td class="tot">{rowtot}</td></tr>')
pfoot = "".join(f'<td class="tot">{pay_comp_tot.get(co,0)}</td>' for co in top_pcomp)
out.append('<h2>7. Heatmap: «Выплаты и проблемы с ними» — подкатегории × компании</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">Крупнейшая категория в разрезе подкатегорий и топ-10 компаний по ней (пополнения исключены).</p>')
out.append(f'<div class="card"><table class="top15 cc-heatmap"><thead><tr><th>#</th><th>Подкатегория</th>{pheads}<th>Всего</th></tr></thead><tbody>{"".join(prows_h)}</tbody><tfoot><tr><td></td><td class="cn">Всего по компании</td>{pfoot}<td class="tot">{sum(pay_comp_tot.get(co,0) for co in top_pcomp)}</td></tr></tfoot></table></div>')

# 9. Deep dive top-15 with subcategories
out.append(f'<h2>8. Детализация по топ-{len(deep)} компаниям</h2>')
out.append(f'<p style="color:#566573;font-size:12px;margin-bottom:8px">По каждой компании — категории и подкатегории ({PERIOD}).</p>')
for c, rows in deep:
    tot = c["total"]
    cs = c["cust"]/tot*100 if tot else 0
    es = c["exec_"]/tot*100 if tot else 0
    # aggregate by category
    by_cat = defaultdict(lambda: {"total": 0, "cust": 0, "exec_": 0, "subs": []})
    for r in rows:
        d = by_cat[r["category"]]
        d["total"] += r["total"]; d["cust"] += r["cust"]; d["exec_"] += r["exec_"]
        if r["subcategory"]:
            d["subs"].append(r)
    body = ""
    for cat, d in sorted(by_cat.items(), key=lambda kv: -kv[1]["total"]):
        body += f'<tr><td>{cat}</td><td>{d["total"]}</td><td>{d["cust"]}</td><td>{d["exec_"]}</td></tr>'
        for s in sorted(d["subs"], key=lambda r: -r["total"]):
            body += f'<tr class="subcat"><td class="sc">{s["subcategory"]}</td><td>{s["total"]}</td><td>{s["cust"]}</td><td>{s["exec_"]}</td></tr>'
    out.append(f'<div class="company-card"><h3>{c["company"]}</h3><div class="stats"><div class="stat"><div class="val">{tot}</div><div class="lbl">всего тикетов</div></div><div class="stat"><div class="val">{cs:.0f}%</div><div class="lbl">от заказчика</div></div><div class="stat"><div class="val">{es:.0f}%</div><div class="lbl">от исполнителя</div></div></div><h4>Категории и подкатегории</h4><table class="d"><thead><tr><th>Категория / подкатегория</th><th>Всего</th><th>Cust</th><th>Exec</th></tr></thead><tbody>{body}</tbody></table></div>')

def _esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 9. Per-ticket deep dive for named companies
out.append('<h2>9. Детальный разбор по тикетам (ключевые компании)</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">Каждый тикет отдельно: категория, подкатегория/детальный бакет и суть проблемы (контекст обращения). «Тикетов» — вес по методологии E (для заказчика = число групп исполнителей в диалоге).</p>')
for co, _variants in DETAIL_COMPANIES:
    rows = company_ticket_detail.get(co, [])
    tot = sum(r["count"] for r in rows)
    cust = sum(r["count"] for r in rows if r["side"] == "customer")
    exec_ = tot - cust
    # per-category subtotal for the header line
    cat_sub = defaultdict(int)
    for r in rows:
        cat_sub[r["category"]] += r["count"]
    cat_line = " · ".join(f'{_esc(k)}: {v}' for k, v in sorted(cat_sub.items(), key=lambda kv: -kv[1]))
    body = ""
    for i, r in enumerate(rows, 1):
        side_label = "заказчик" if r["side"] == "customer" else "исполнитель"
        sub = r["subcategory"] or "—"
        detail = _esc(sub)
        if r["fine_bucket"] and r["fine_bucket"] != sub:
            detail += f' <span class="fd">→ {_esc(r["fine_bucket"])}</span>'
        body += (
            f'<tr><td class="rank">{i}</td><td>{r["dialog_id"]}</td>'
            f'<td>{r["dd"].strftime("%d.%m")}</td><td>{side_label}</td>'
            f'<td style="text-align:center;font-weight:700">{r["count"]}</td>'
            f'<td>{_esc(r["category"])}</td><td style="font-size:10px">{detail}</td>'
            f'<td style="font-size:10.5px">{_esc(r["summary"])}</td></tr>'
        )
    out.append(
        f'<div class="company-card"><h3>{_esc(co)}</h3>'
        f'<div class="stats"><div class="stat"><div class="val">{tot}</div><div class="lbl">всего тикетов</div></div>'
        f'<div class="stat"><div class="val">{cust}</div><div class="lbl">от заказчика</div></div>'
        f'<div class="stat"><div class="val">{exec_}</div><div class="lbl">от исполнителя</div></div></div>'
        f'<p style="color:#566573;font-size:11px;margin:0 0 6px">Разбивка: {cat_line}.</p>'
        f'<table class="top15 pc"><thead><tr><th>#</th><th>Диалог</th><th>Дата</th><th>Сторона</th><th>Тикетов</th><th>Категория</th><th>Подкатегория / бакет</th><th>Проблема</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>')

# 10. Potential clients standalone table
out.append('<h2>10. Потенциальные заказчики</h2>')
out.append(f'<p style="color:#566573;font-size:12px;margin-bottom:8px">Отдельный список тикетов категории «Потенциальный клиент» ({PERIOD}) — система, идентификатор диалога и контекст обращения. Всего: {len(potential)}.</p>')
prows = ""
for i, r in enumerate(potential, 1):
    sys_label = "ChatApp (Telegram)" if r["source"] == "chatapp" else "Flomni"
    side_label = "заказчик" if r["side"] == "customer" else "исполнитель"
    if r["source"] == "chatapp":
        ident = f'chat {_esc(r["client_id"])}'
        if r["chat_name"]:
            ident += f' · {_esc(r["chat_name"])}'
    else:
        ident = _esc(r["client_id"])
    prows += (
        f'<tr><td class="rank">{i}</td><td>{r["dialog_id"]}</td><td>{sys_label}</td>'
        f'<td>{side_label}</td><td>{r["dd"].strftime("%d.%m")}</td><td>{_esc(r["company"])}</td>'
        f'<td style="font-family:monospace;font-size:10px">{ident}</td>'
        f'<td style="font-size:10.5px">{_esc(r["summary"])}</td></tr>'
    )
out.append(f'<div class="card"><table class="top15 pc"><thead><tr><th>#</th><th>Диалог</th><th>Система</th><th>Сторона</th><th>Дата</th><th>Компания</th><th>Идентификатор</th><th>Контекст</th></tr></thead><tbody>{prows}</tbody></table></div>')

# 11. Repeat contacts
out.append('<h2>11. Повторные обращения</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">Обращение закрыли, а человек '
           'вернулся с тем же вопросом. Этого не видят ни скорость первого ответа, ни отметка '
           '«решено»: быстрый ответ, который не помог, выглядит там как успех. Метрика показывает '
           'категории, где ломается процесс.</p>')
out.append(rc.render_html(rc.compute(DATE_FROM, DATE_TO)))

out.append(f'<div class="disclaimer">Методология E: 1 тикет = 1 диалог для executor; для customer — число групп email исполнителей (связные компоненты: email из одного сообщения объединяются в одну группу, из разных сообщений — считаются отдельно, мин. 1). Сторона определяется комбинированным правилом: групповой чат либо ≥2 разных email исполнителей → заказчик, иначе исполнитель. Диалог квалифицируется как тикет при наличии хотя бы одного содержательного входящего сообщения либо категории KYC (порядок хранения сообщений не важен). Исключены: «Тест», «Другое», внутренние клиенты, рассылки (обновление API ФНС «citizenship», «Дата принятия задачи»), автоматические фиды (Wallet-транзакции), сервисный шум, кросс-кабинетные дубли Flomni. Подкатегории заданы для «Выплат», «KYC», «Запроса документов», «Вопросов по работе» и «Технической проблемы»; для «Выплат» и «KYC» дана детализация по fine-бакетам (первое совпадение регулярного правила). Бакет «Пополнение/депозит: зачисление на баланс платформы (заказчик)» вынесен в отдельную категорию «Пополнение и зачисление на баланс». Данные за {PERIOD}.</div>')
out.append(f'<div class="footer"><span><b>MadeTask</b> × Remozo · Аналитика поддержки</span><span>Отчёт за {PERIOD} · Методология E · Конфиденциально</span></div>')
out.append('</body></html>')

print("\n".join(out))
