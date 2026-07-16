"""
Ad-hoc отчёт за 1–30 июня 2026 по методологии D из live-БД (таблица tickets +
последний AnalysisResult для подкатегорий). Печатает HTML в stdout.

Запуск (из контейнера, читает БД, пишет HTML на хост):
  docker exec -i support_tickets-scheduler-1 python - < build_june_report.py > out.html
"""
from collections import defaultdict

from sqlalchemy import text
from database import engine

DATE_FROM, DATE_TO = "2026-06-01", "2026-06-30"
SUBCAT_CATEGORIES = (
    "Выплаты и проблемы с ними",
    "KYC",
    "Запрос документов",
)
WEEKS = [
    ("01-07 июня", "2026-06-01", "2026-06-07"),
    ("08-14 июня", "2026-06-08", "2026-06-14"),
    ("15-21 июня", "2026-06-15", "2026-06-21"),
    ("22-30 июня", "2026-06-22", "2026-06-30"),
]


def q(sql, **p):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings().all()]


P = {"a": DATE_FROM, "b": DATE_TO}

summary = q("""
    SELECT COALESCE(SUM(count),0) total,
           COALESCE(SUM(count) FILTER (WHERE side='customer'),0) customer,
           COALESCE(SUM(count) FILTER (WHERE side='executor'),0) executor,
           COUNT(DISTINCT dialog_id) dialogs
      FROM tickets WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
""", **P)[0]

by_source = q("""
    SELECT source, SUM(count) n FROM tickets
     WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
     GROUP BY source ORDER BY n DESC
""", **P)

cats = q("""
    SELECT category, SUM(count) total,
           COALESCE(SUM(count) FILTER (WHERE side='customer'),0) cust,
           COALESCE(SUM(count) FILTER (WHERE side='executor'),0) exec_
      FROM tickets WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
     GROUP BY category ORDER BY total DESC
""", **P)

companies = q("""
    SELECT company, SUM(count) total,
           COALESCE(SUM(count) FILTER (WHERE side='customer'),0) cust,
           COALESCE(SUM(count) FILTER (WHERE side='executor'),0) exec_
      FROM tickets WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
       AND company IS NOT NULL AND company<>'' AND company<>'Unknown'
     GROUP BY company ORDER BY total DESC LIMIT 15
""", **P)

# Subcategory detail: join latest AR for subcategory
subcats = q("""
    SELECT t.category, ar.subcategory,
           SUM(t.count) total,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
           COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_
      FROM tickets t
      JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
           ON l.dialog_id=t.dialog_id
      JOIN analysis_results ar ON ar.id=l.mx
     WHERE t.methodology='D' AND t.dialog_date BETWEEN :a AND :b
       AND t.category = ANY(:cats)
     GROUP BY t.category, ar.subcategory
""", cats=list(SUBCAT_CATEGORIES), **P)

# Weeks
weeks_data = []
for label, a, b in WEEKS:
    r = q("""
        SELECT COALESCE(SUM(count),0) tickets, COUNT(DISTINCT dialog_id) dialogs
          FROM tickets WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
    """, a=a, b=b)[0]
    weeks_data.append((label, a, b, r["dialogs"], r["tickets"]))

# Heatmap cats × companies (top10 each)
top10_comp = [c["company"] for c in companies[:10]]
top10_cat = [c["category"] for c in cats[:10]]
cc_rows = q("""
    SELECT company, category, SUM(count) n FROM tickets
     WHERE methodology='D' AND dialog_date BETWEEN :a AND :b
       AND company = ANY(:cs) AND category = ANY(:ks)
     GROUP BY company, category
""", cs=top10_comp, ks=top10_cat, **P)
cc = {(r["company"], r["category"]): r["n"] for r in cc_rows}

# Deep dive top-5 companies: category + subcategory
deep = []
for c in companies[:5]:
    rows = q("""
        SELECT t.category, ar.subcategory,
               SUM(t.count) total,
               COALESCE(SUM(t.count) FILTER (WHERE t.side='customer'),0) cust,
               COALESCE(SUM(t.count) FILTER (WHERE t.side='executor'),0) exec_
          FROM tickets t
          JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
               ON l.dialog_id=t.dialog_id
          JOIN analysis_results ar ON ar.id=l.mx
         WHERE t.methodology='D' AND t.dialog_date BETWEEN :a AND :b
           AND t.company=:co
         GROUP BY t.category, ar.subcategory
    """, co=c["company"], **P)
    deep.append((c, rows))

# Potential clients: standalone list (one row per ticket)
potential = q("""
    SELECT t.dialog_id, t.source, t.side, t.dialog_date::date dd,
           COALESCE(NULLIF(t.company,''),'—') company,
           d.client_id, d.license_id, d.messenger_type, d.chat_name,
           ar.summary
      FROM tickets t
      JOIN dialogs d ON d.id=t.dialog_id
      JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
           ON l.dialog_id=t.dialog_id
      JOIN analysis_results ar ON ar.id=l.mx
     WHERE t.methodology='D' AND t.category='Потенциальный клиент'
       AND t.dialog_date BETWEEN :a AND :b
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


CSS = """@page{size:A4;margin:18mm 12mm}body{font-family:-apple-system,"Segoe UI",Arial,sans-serif;color:#2C3E50;line-height:1.4}h1{color:#1A2935;font-size:26px;margin:0 0 4px}h2{color:#34495E;font-size:18px;border-left:4px solid #3498DB;padding-left:10px;margin:24px 0 12px;page-break-after:avoid}h3{color:#2C3E50;font-size:16px;margin:18px 0 8px}h4{color:#34495E;font-size:13px;margin:10px 0 6px}.hero{background:linear-gradient(135deg,#E74C3C,#922B21);color:#fff;padding:18px 22px;border-radius:8px;margin-bottom:18px}.hero .sub{opacity:.85;font-size:13px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}.kpi{background:#fff;border:1px solid #E6EAEE;border-radius:6px;padding:10px 12px;text-align:center}.kpi .v{font-size:24px;font-weight:700;color:#3498DB}.kpi .l{font-size:10px;text-transform:uppercase;color:#7f8c8d;margin-top:2px}.card{border:1px solid #E6EAEE;border-radius:6px;padding:14px 16px;margin-bottom:14px;background:#fff}table{width:100%;border-collapse:collapse;font-size:11px}th{background:#34495E;color:#fff;padding:6px 8px;text-align:left;font-weight:600}td{padding:5px 8px;border-bottom:1px solid #ECF0F1}table.top15 td.rank{text-align:center;font-weight:700;color:#7f8c8d;width:30px}table.top15 td.cn{font-weight:600;max-width:220px}table.top15 td.tot{font-weight:700;background:#34495E;color:#fff;text-align:center}table.top15 td{text-align:center}.company-card{background:#FBFCFD;border:1px solid #DCE3EA;border-radius:8px;padding:14px 18px;margin-bottom:18px}.company-card h3{font-size:18px;margin-top:0;color:#1A2935}.stats{display:flex;gap:16px;margin:12px 0}.stat{flex:1;text-align:center;background:#fff;border:1px solid #E6EAEE;border-radius:6px;padding:10px}.stat .val{font-size:24px;font-weight:700;color:#3498DB}.stat .lbl{font-size:10px;color:#7f8c8d;text-transform:uppercase;margin-top:2px}table.d{font-size:10px}.subcat td.sc{padding-left:24px;color:#566573;font-style:italic}.subcat td.sc::before{content:"\\21B3 ";color:#95A5A6}.disclaimer{font-size:9px;color:#95A5A6;font-style:italic;margin-top:14px}.legend{display:flex;gap:14px;font-size:11px;margin:8px 0 0;color:#7F8C8D}.legend .sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px}.row-flex{display:flex;gap:16px;align-items:center}.row-flex>div:first-child{flex-shrink:0}table.top15 th.rot{writing-mode:vertical-rl;transform:rotate(180deg);padding:8px 4px;font-size:10px;height:140px;vertical-align:bottom;white-space:nowrap;max-width:32px}table.cc-heatmap td.cn{max-width:280px;font-size:10.5px}table.cc-heatmap th{font-size:10.5px}table.cc-heatmap tfoot td{background:#34495E;color:#fff;font-weight:700}table.cc-heatmap tfoot td.cn{background:#2C3E50}.week-bars{display:flex;gap:8px;align-items:flex-end;height:120px;margin:12px 0 4px}.week-bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}.week-bar{width:100%;background:#3498DB;border-radius:4px 4px 0 0;display:flex;align-items:flex-start;justify-content:center;padding-top:4px;color:#fff;font-size:10px;font-weight:700}.week-label{font-size:9px;color:#7f8c8d;text-align:center}.subcat-block{background:#F8F9FA;border-left:3px solid #3498DB;padding:8px 14px;margin:10px 0}.subcat-block h4{margin:0 0 6px;color:#1A2935}table.pc td{text-align:left;vertical-align:top}table.pc td.rank{text-align:center}table.pc th{text-align:left}"""

out = []
out.append('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Июнь 1-30 2026</title><style>' + CSS + '</style></head><body>')
out.append('<div class="hero"><h1>Июнь 1–30 2026 — детальный анализ</h1><div class="sub">Период: 01.06.2026 – 30.06.2026 · Источники: ChatApp + Flomni · Методология D · с детализацией по подкатегориям</div></div>')
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
out.append(f'<h2>3. Топ-{len(companies)} компаний (01–30 июня)</h2>')
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
out.append('<h2>5. Детализация по подкатегориям</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">Разбивка тикетов внутри категорий, у которых заданы подкатегории.</p>')
DISPLAY_ORDER = ["Выплаты и проблемы с ними", "KYC", "Запрос документов"]
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
    out.append(f'<div class="subcat-block"><h4>{cat} · {ctot} тикетов</h4><table class="d"><thead><tr><th>Подкатегория</th><th>Всего</th><th>Cust</th><th>Exec</th><th>% категории</th></tr></thead><tbody>{body}</tbody></table></div>')

# 6. Heatmap
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

# 7. Deep dive top-5 with subcategories
out.append('<h2>7. Детализация по топ-5 компаниям</h2>')
out.append('<p style="color:#566573;font-size:12px;margin-bottom:8px">По каждой компании — категории и подкатегории (1–30 июня).</p>')
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

# 8. Potential clients standalone table
def _esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


out.append('<h2>8. Потенциальные заказчики</h2>')
out.append(f'<p style="color:#566573;font-size:12px;margin-bottom:8px">Отдельный список тикетов категории «Потенциальный клиент» (1–30 июня) — система, идентификатор диалога и контекст обращения. Всего: {len(potential)}.</p>')
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

# 9. Email support requests (external data from support team, NOT in main counter)
EMAIL_EXECUTOR = [
    ("Статус ИП РФ/Самозанятого", 1, 9),
    ("Выплаты и проблемы с ними", 5, 3),
    ("KYC", 4, 3),
    ("Вопросы по работе в сервисе", 5, 1),
    ("Изменение/удаление аккаунта", 5, 1),
    ("SEPA/SWIFT", 4, 1),
    ("Техническая проблема/вопрос", 5, 0),
    ("Запрос документов", 1, 2),
    ("Функциональность сервиса и возможности выплат", 1, 0),
    ("Потенциальный клиент", 1, 0),
    ("Другое", 1, 0),
]
EMAIL_CUSTOMER = [
    ("Другое", 2, 1),
    ("Вопросы по числам отправки закрывашек в ЭДО / поторопить бухгалтерию", 1, 0),
    ("Изменения в профиле заказчика/исполнителя", 1, 0),
]


def _email_table(rows):
    tm = sum(r[1] for r in rows)
    tr = sum(r[2] for r in rows)
    body = ""
    for cat, mt, rm in rows:
        body += (
            f'<tr><td>{_esc(cat)}</td>'
            f'<td class="rank">{mt}</td><td class="rank">{rm}</td>'
            f'<td class="rank"><b>{mt + rm}</b></td></tr>'
        )
    body += (
        f'<tr style="font-weight:700;background:#f4f6f7"><td>Итого</td>'
        f'<td class="rank">{tm}</td><td class="rank">{tr}</td>'
        f'<td class="rank">{tm + tr}</td></tr>'
    )
    return (
        '<div class="card"><table class="top15 pc">'
        '<thead><tr><th>Категория</th><th>Почта madetask</th>'
        '<th>Почта Remozo</th><th>Всего</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


_e_tot = sum(r[1] + r[2] for r in EMAIL_EXECUTOR)
_c_tot = sum(r[1] + r[2] for r in EMAIL_CUSTOMER)
_mt_tot = sum(r[1] for r in EMAIL_EXECUTOR + EMAIL_CUSTOMER)
_rm_tot = sum(r[2] for r in EMAIL_EXECUTOR + EMAIL_CUSTOMER)
out.append('<h2>9. Запросы поддержки по почте</h2>')
out.append(
    '<p style="color:#566573;font-size:12px;margin-bottom:8px">Данные отдела поддержки по обращениям в почтовые ящики (madetask и Remozo) за 1–30 июня. '
    f'<b>В основной счётчик тикетов не включаются.</b> Всего: {_e_tot + _c_tot} '
    f'(madetask {_mt_tot} · Remozo {_rm_tot}).</p>'
)
out.append(f'<h3 style="margin:14px 0 6px">Запросы от исполнителей — {_e_tot}</h3>')
out.append(_email_table(EMAIL_EXECUTOR))
out.append(f'<h3 style="margin:14px 0 6px">Запросы от заказчиков — {_c_tot}</h3>')
out.append(_email_table(EMAIL_CUSTOMER))

out.append('<div class="disclaimer">Методология D: 1 тикет = 1 диалог для executor; для customer — количество уникальных email исполнителей (мин. 1). Исключены: «Тест», внутренние клиенты, рассылки, кросс-кабинетные дубли Flomni, outbound-first диалоги (кроме KYC). Подкатегории заданы для «Выплат», «KYC» и «Запроса документов». Данные на 30.06.2026.</div>')
out.append('</body></html>')

print("\n".join(out))
