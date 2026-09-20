"""Сводный HTML-отчёт по работе поддержки за несколько месяцев.

Считает то же, что manager_report.py, но собирает три месяца сразу и рисует
интерактивную страницу: переключение метрик, тултипы, сортировка таблиц.

Метрики отделов вынесены в отдельный блок и считаются ПО ИНЦИДЕНТАМ, каждый
ровно один раз. В разрезе менеджеров тот же инцидент приписан всем, кто в нём
работал, поэтому те два блока складывать между собой нельзя.

  python build_manager_report.py --months 2026-06 2026-07 2026-08 -o report.html
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import text

from database import get_session
import repeat_contacts as rc
import resolution_metrics as rm
from manager_report import build_ts_map, short

# Линия поддержки. Все, кого нет в списке, из отчёта выпадают: это сотрудники
# смежных команд и разовые заходы, они смазывают картину по людям.
INCLUDE_OPS = {
    "ekozlova", "maleksandrov", "ayaglenko", "abelyakova", "atatyanina",
    "azaporozhets", "mgazizova", "tvakhitov", "sgavrilenko", "fedotova",
}

# Общий аккаунт ChatApp. За ним живой человек, но кто именно, в данных нет.
# Держим отдельной строкой, иначе весь июнь по этому каналу просто исчезнет.
SHARED_LABEL = "RemozoSupport*"

# Нормативы SLA: сегмент -> (первый ответ, решение), в секундах рабочего времени.
# Сегмент заказчика берётся по обороту закрытых задач за предыдущий месяц:
# High > 10 млн ₽, Medium 3–10 млн ₽, Low < 3 млн ₽ (company_turnover_segment).
SLA_NORMS = {
    "High": (10 * 60, 1 * 3600),
    "Medium": (30 * 60, 2 * 3600),
    "Low": (30 * 60, 8 * 3600),
    "Исполнители": (30 * 60, 4 * 3600),
}
SEG_ORDER = ["High", "Medium", "Low", "Исполнители"]

MONTH_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь",
    12: "Декабрь",
}

DEPT_RU = {
    "compliance": "Комплаенс",
    "finance": "Финотдел",
    "documents": "Документооборот",
    "legal": "Юридический",
    "bank_provider": "Банк / провайдер",
    "technical": "Технический",
}

def keep(name: str) -> bool:
    return name in INCLUDE_OPS or name == SHARED_LABEL


def collect(y: int, mo: int) -> dict:
    """Считает один месяц: срез по менеджерам и срез по отделам."""
    lo = date(y, mo, 1)
    hi = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)

    # Сегмент действует по обороту за месяц ДО отчётного: он должен быть известен
    # заранее, а не считаться задним числом по тем же дням, что мы оцениваем.
    seg_month = f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"

    with get_session() as db:
        rows = db.execute(text("""
            SELECT d.id, d.client_id, d.messages_json, tk.side, cs.segment
            FROM dialogs d
            -- Сторона обращения нужна для сегмента: исполнители идут своим
            -- нормативом, а у заказчиков он зависит от оборота компании.
            JOIN (SELECT dialog_id, min(side) AS side FROM tickets
                   WHERE dialog_date >= :df AND dialog_date < :dt
                     AND methodology = 'E'
                   GROUP BY dialog_id) tk ON tk.dialog_id = d.id
            LEFT JOIN company_turnover_segment cs
                   ON cs.month = :sm
                  AND cs.platform = d.company_platform
                  AND cs.company_id = d.company_id
            WHERE d.messages_json IS NOT NULL
              AND d.messages_json <> '' AND d.messages_json <> '[]'
              -- Теневые дубли: тот же TG-чат, пришедший вторым каналом Flomni.
              -- Без этого фильтра часть обращений считается дважды.
              AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                              WHERE s.dialog_id = d.id)
            ORDER BY d.id
        """), {"df": lo.isoformat(), "dt": hi.isoformat(), "sm": seg_month}).fetchall()

    inc_total = 0
    last_day = None
    fr_unknown = 0

    # по менеджерам
    touched = Counter()
    firstresp = Counter()
    msgs = Counter()
    clients = defaultdict(set)
    fr_secs = defaultdict(list)
    handoff_inc = Counter()

    # по отделам: каждый инцидент учитывается один раз
    dept_secs = defaultdict(list)
    dept_all = Counter()
    dept_closed = Counter()
    dept_open = Counter()
    inc_with_handoff = 0
    inc_pending = 0

    # SLA: сегмент -> менеджер -> списки секунд рабочего времени.
    # Ключ "" держит сводку по сегменту целиком, включая неопознанных операторов.
    sla = defaultdict(lambda: defaultdict(lambda: {"frt": [], "ort": []}))
    sla_inc = Counter()
    sla_skipped = 0

    for _did, cid, mj, side, segment in rows:
        try:
            raw = json.loads(mj or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, list):
            continue

        tsmap, _ = build_ts_map(raw)
        norm = rm.normalize(raw)
        # Заказчик без компании остаётся без норматива, и в блок SLA он не идёт:
        # иначе строка «сегмент неизвестен» соберёт 11% инцидентов и будет
        # выглядеть как ещё один сегмент, для которого просто забыли норму.
        sla_seg = "Исполнители" if side == "executor" else segment

        for idx, seg in enumerate(rm.segment_incidents(norm)):
            met = rm.compute_incident(seg, idx)
            if met.started_at is None:
                continue
            if (met.started_at.year, met.started_at.month) != (y, mo):
                continue
            inc_total += 1
            d = met.started_at.date()
            last_day = d if last_day is None or d > last_day else last_day

            # ── срез по отделам ──
            # Время считаем по закрытым передачам и складываем по одному разу
            # на инцидент. Счётчики передач ведём по событиям и отдельно от
            # met.status: инцидент бывает решён с висящей передачей, и тогда
            # он в pending_department не попадает, а передача всё равно открыта.
            if met.handoff_seconds_by_dept:
                inc_with_handoff += 1
                for dep, sec in met.handoff_seconds_by_dept.items():
                    dept_secs[dep].append(sec)
            for h in met.handoffs:
                dept_all[h.department] += 1
                if h.closed_at is None:
                    dept_open[h.department] += 1
                else:
                    dept_closed[h.department] += 1
            if met.status == "pending_department":
                inc_pending += 1

            # ── срез по менеджерам ──
            here = set()
            for x in seg:
                if not (x.is_support and x.substantive):
                    continue
                who = tsmap.get(x.ts)
                if not who or x.text.strip().startswith("/"):
                    continue
                name = short(who)
                if not keep(name):
                    continue
                here.add(name)
                msgs[name] += 1
                clients[name].add(cid)

            for name in here:
                touched[name] += 1
                if met.handoff_seconds_by_dept:
                    handoff_inc[name] += 1

            if met.first_response_at is not None:
                who = tsmap.get(met.first_response_at)
                name = short(who) if who else None
                if name and keep(name):
                    firstresp[name] += 1
                    if met.first_response_seconds is not None:
                        fr_secs[name].append(met.first_response_seconds)
                elif who is None:
                    fr_unknown += 1

            # ── срез по SLA ──
            if sla_seg is None:
                sla_skipped += 1
                continue
            sla_inc[sla_seg] += 1
            # Обе метрики в рабочих секундах (Пн–Пт 10:00–19:00 MSK): норматив
            # в 8 часов для Low имеет смысл только в графике поддержки.
            if met.first_response_at is not None:
                frt = rm.working_seconds(met.started_at, met.first_response_at)
                who = tsmap.get(met.first_response_at)
                name = short(who) if who else None
                sla[sla_seg][""]["frt"].append(frt)
                if name and keep(name):
                    sla[sla_seg][name]["frt"].append(frt)
            if met.resolved_at is not None and met.resolution_working_seconds is not None:
                who = tsmap.get(met.resolved_at)
                name = short(who) if who else None
                sla[sla_seg][""]["ort"].append(met.resolution_working_seconds)
                if name and keep(name):
                    sla[sla_seg][name]["ort"].append(met.resolution_working_seconds)

    def med(a):
        return statistics.median(a) if a else None

    def avg(a):
        return statistics.fmean(a) if a else None

    managers = []
    for name in sorted(touched, key=lambda n: (-touched[n], n)):
        managers.append({
            "name": name,
            "incidents": touched[name],
            "first": firstresp[name],
            "frt_med": med(fr_secs[name]),
            "frt_avg": avg(fr_secs[name]),
            "handoffs": handoff_inc[name],
            "messages": msgs[name],
            "clients": len(clients[name]),
        })

    depts = []
    # Идём по всем отделам, куда была хоть одна передача, а не только по тем,
    # где есть закрытые. Иначе отдел, из которого ни разу не вернулись, просто
    # пропадает из таблицы и выглядит как «туда не обращались».
    for dep in sorted(dept_all, key=lambda d: -dept_all[d]):
        vals = dept_secs.get(dep) or []
        depts.append({
            "dept": dep,
            "label": DEPT_RU.get(dep, dep),
            "n": dept_all[dep],
            "closed": dept_closed.get(dep, 0),
            "open": dept_open.get(dep, 0),
            "total": sum(vals),
            "avg": avg(vals),
            "med": med(vals),
            "max": max(vals) if vals else None,
        })

    def sla_cell(vals, limit):
        """Медиана и доля в норме. Без замеров возвращаем None, а не ноль:
        ноль читался бы как «всё просрочено»."""
        if not vals:
            return {"n": 0, "med": None, "ok": None}
        return {"n": len(vals),
                "med": med(vals),
                "ok": sum(1 for v in vals if v <= limit) / len(vals) * 100}

    sla_out = {}
    for seg_name in SEG_ORDER:
        if seg_name not in sla:
            continue
        frt_lim, ort_lim = SLA_NORMS[seg_name]
        by_mgr = {}
        for who, vals in sla[seg_name].items():
            by_mgr[who] = {"frt": sla_cell(vals["frt"], frt_lim),
                           "ort": sla_cell(vals["ort"], ort_lim)}
        sla_out[seg_name] = {"inc": sla_inc[seg_name],
                             "frt_norm": frt_lim, "ort_norm": ort_lim,
                             "mgr": by_mgr}

    all_frt = [v for lst in fr_secs.values() for v in lst]
    return {
        "key": f"{y}-{mo:02d}",
        "seg_month": seg_month,
        "sla": sla_out,
        "sla_skipped": sla_skipped,
        "label": f"{MONTH_RU[mo]} {y}",
        "incidents": inc_total,
        "last_day": last_day.isoformat() if last_day else None,
        "days": (last_day - lo).days + 1 if last_day else 0,
        "fr_unknown": fr_unknown,
        "frt_med_all": med(all_frt),
        "inc_with_handoff": inc_with_handoff,
        "inc_pending": inc_pending,
        "managers": managers,
        "depts": depts,
    }


def fmt(seconds) -> str:
    """Секунды в короткую человеческую строку."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м {s % 60:02d}с"
    if s < 86400:
        return f"{s // 3600}ч {(s % 3600) // 60:02d}м"
    return f"{s // 86400}д {(s % 86400) // 3600}ч"


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif;
 background:#F1F5F9;color:#0F172A;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px 64px}
.hero{background:linear-gradient(135deg,#0F172A 0%,#1E293B 55%,#334155 100%);color:#fff;
 padding:38px 0 34px;margin-bottom:26px}
.hero .wrap{padding-bottom:0}
.brand{display:flex;align-items:center;gap:10px;font-size:12px;letter-spacing:.14em;
 text-transform:uppercase;color:#94A3B8;margin-bottom:14px}
.brand b{color:#fff;font-weight:600;letter-spacing:.14em}
.dot{width:4px;height:4px;border-radius:50%;background:#475569}
h1{font-size:30px;font-weight:650;letter-spacing:-.02em;margin-bottom:8px}
.sub{color:#94A3B8;font-size:13.5px;max-width:780px}
h2{font-size:19px;font-weight:640;letter-spacing:-.01em;margin:34px 0 4px;
 display:flex;align-items:center;gap:10px}
h2 .num{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;
 border-radius:7px;background:#0F172A;color:#fff;font-size:12px;font-weight:600}
.lead{color:#64748B;font-size:13px;margin:0 0 14px 34px;max-width:820px}
.card{background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:20px 22px;
 box-shadow:0 1px 2px rgba(15,23,42,.04)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:-52px}
.kpi{background:#fff;border:1px solid #E2E8F0;border-radius:14px;padding:16px 18px;
 box-shadow:0 4px 14px rgba(15,23,42,.08)}
.kpi .v{font-size:27px;font-weight:660;letter-spacing:-.02em}
.kpi .l{font-size:11.5px;color:#64748B;margin-top:2px}
.kpi .d{font-size:11.5px;margin-top:6px;font-weight:560}
.up{color:#059669}.down{color:#DC2626}.flat{color:#94A3B8}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:right;font-weight:580;color:#475569;font-size:11.5px;text-transform:uppercase;
 letter-spacing:.04em;padding:0 10px 9px;border-bottom:1px solid #E2E8F0;white-space:nowrap;
 cursor:pointer;user-select:none}
th:first-child{text-align:left}
th:hover{color:#0F172A}
th.sorted::after{content:' \\25BE';color:#6366F1}
th.asc::after{content:' \\25B4';color:#6366F1}
td{text-align:right;padding:9px 10px;border-bottom:1px solid #F1F5F9;
 font-variant-numeric:tabular-nums}
td:first-child{text-align:left;font-weight:560}
tbody tr:hover{background:#F8FAFC}
tbody tr:last-child td{border-bottom:none}
.muted{color:#94A3B8}
.print-only{display:none}
.bar-cell{width:96px}
.mini{height:7px;border-radius:4px;background:#EEF2F7;overflow:hidden}
.mini i{display:block;height:100%;border-radius:4px;background:#6366F1}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px}
.seg{display:inline-flex;background:#EEF2F7;border-radius:9px;padding:3px}
.seg button{border:0;background:transparent;font:inherit;font-size:12.5px;font-weight:560;
 color:#475569;padding:6px 13px;border-radius:7px;cursor:pointer}
.seg button.on{background:#fff;color:#0F172A;box-shadow:0 1px 3px rgba(15,23,42,.12)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:#475569;margin-top:12px}
.legend span{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block}
.legend .off{opacity:.32}
.chart{width:100%;overflow:visible}
.chart .grid line{stroke:#E9EEF5;stroke-width:1}
.chart .axis{fill:#94A3B8;font-size:11px}
.chart .lbl{fill:#334155;font-size:11.5px;font-weight:540}
.chart rect.b{cursor:pointer;transition:opacity .12s}
.chart rect.b:hover{opacity:.82}
#tip{position:fixed;pointer-events:none;background:#0F172A;color:#fff;font-size:12px;
 border-radius:8px;padding:8px 11px;opacity:0;transition:opacity .1s;z-index:99;
 box-shadow:0 6px 20px rgba(15,23,42,.28);white-space:nowrap}
#tip b{font-weight:620}
#tip .r{color:#94A3B8}
.note{background:#FFFBEB;border:1px solid #FDE68A;border-left:3px solid #F59E0B;
 border-radius:9px;padding:13px 16px;font-size:12.5px;color:#78350F;margin-top:14px}
.note b{font-weight:620}
.note ul{margin:7px 0 0 18px}
.note li{margin:3px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.foot{margin-top:34px;padding-top:18px;border-top:1px solid #E2E8F0;
 color:#94A3B8;font-size:11.5px}
tr.grp td{background:#F1F5F9;font-weight:640;font-size:12px;color:#0F172A;
 text-transform:uppercase;letter-spacing:.04em;padding:7px 10px}
/* Альбомная ориентация: отчёт широкий, в портрет таблицы не влезают
   и правые колонки обрезаются. */
@media print{
 @page{size:A4 landscape;margin:10mm}
 body{background:#fff}
 .wrap{max-width:none;padding:0}
 .hero,.kpi,.mini i,.grp td,svg .b{-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .card,.kpi{box-shadow:none}
 /* Заголовок не должен отрываться от своей карточки. Разрывать карточку
    целиком нельзя только у графиков, таблицы обязаны уметь переноситься. */
 h2,.lead{break-after:avoid;break-inside:avoid}
 h2{margin-top:22px}
 .lead{margin-bottom:10px}
 .card:has(svg){break-inside:avoid}
 tr{break-inside:avoid}
 thead{display:table-header-group}
 tr.grp{break-after:avoid}
 table{font-size:11.5px}
 th{font-size:10px;padding:0 7px 7px}
 td{padding:6px 7px}
 .mini{display:none}
 /* Переключатели в PDF не нажать. У графиков оставляем активный: он работает
    подписью, иначе непонятно, какая метрика нарисована. Над таблицей убираем
    совсем, там на печати выведены все месяцы сразу. */
 .seg{background:none;padding:0}
 .seg button{display:none}
 .seg button.on{display:inline-block;background:none;box-shadow:none;
  color:#475569;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:0 0 6px}
 #segTbl,.screen-only{display:none}
 .print-only{display:inline}
 #tip{display:none}
}
"""

JS = r"""
let PRINT=false;
const T=document.getElementById('tip');
function tip(e,html){T.innerHTML=html;T.style.opacity=1;
 let x=e.clientX+14,y=e.clientY-10;
 if(x+T.offsetWidth>innerWidth-8)x=e.clientX-T.offsetWidth-14;
 T.style.left=x+'px';T.style.top=y+'px';}
function untip(){T.style.opacity=0;}

const SVG='http://www.w3.org/2000/svg';
function el(n,a){const e=document.createElementNS(SVG,n);
 for(const k in a)e.setAttribute(k,a[k]);return e;}

/* Горизонтальный сгруппированный бар-чарт.
   rows: [{label, vals:[{series,value,tipHtml}]}] */
function groupedBar(host,rows,series,fmtv){
 host.innerHTML='';
 const on=series.filter(s=>!s.off);
 if(!rows.length||!on.length){host.innerHTML='<p class="muted" style="font-size:12.5px">Нет данных</p>';return;}
 // На печати сжимаем: иначе график выше страницы A4 и Chrome рвёт его пополам.
 const BH=PRINT?11:15,GAP=PRINT?9:16;
 const padL=132,padR=54,padT=6,rowH=on.length*BH+GAP,H=padT+rows.length*rowH+18;
 const W=host.clientWidth||880,innerW=W-padL-padR;
 const max=Math.max(...rows.flatMap(r=>r.vals.filter(v=>on.some(s=>s.key===v.series)).map(v=>v.value)),1);
 const svg=el('svg',{class:'chart',viewBox:`0 0 ${W} ${H}`,width:'100%',height:H});
 const g=el('g',{class:'grid'});
 for(let i=0;i<=4;i++){const x=padL+innerW*i/4;
  g.appendChild(el('line',{x1:x,y1:padT,x2:x,y2:H-18}));
  const t=el('text',{x:x,y:H-4,class:'axis','text-anchor':'middle'});
  t.textContent=fmtv(max*i/4);g.appendChild(t);}
 svg.appendChild(g);
 rows.forEach((r,ri)=>{
  const y0=padT+ri*rowH;
  const lb=el('text',{x:padL-10,y:y0+rowH/2+1,class:'lbl','text-anchor':'end'});
  lb.textContent=r.label;svg.appendChild(lb);
  on.forEach((s,si)=>{
   const v=r.vals.find(v=>v.series===s.key);if(!v)return;
   const w=Math.max(v.value/max*innerW,v.value>0?2:0);
   const y=y0+(GAP/2)+si*BH;
   const rect=el('rect',{class:'b',x:padL,y:y,width:w,height:BH-4,rx:3,fill:s.color});
   rect.addEventListener('mousemove',e=>tip(e,v.tipHtml));
   rect.addEventListener('mouseleave',untip);
   svg.appendChild(rect);
   if(v.value>0){const t=el('text',{x:padL+w+6,y:y+BH-5,class:'axis'});
    t.textContent=fmtv(v.value);svg.appendChild(t);}
  });
 });
 host.appendChild(svg);
}

function sortable(tbl){
 tbl.querySelectorAll('th').forEach((th,i)=>{
  th.addEventListener('click',()=>{
   const tb=tbl.tBodies[0],rows=[...tb.rows];
   const asc=!th.classList.contains('sorted');
   tbl.querySelectorAll('th').forEach(h=>h.classList.remove('sorted','asc'));
   th.classList.add(asc?'asc':'sorted');
   rows.sort((a,b)=>{
    const x=a.cells[i].dataset.v??a.cells[i].textContent;
    const y=b.cells[i].dataset.v??b.cells[i].textContent;
    const nx=parseFloat(x),ny=parseFloat(y);
    if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;
    return asc?String(x).localeCompare(y,'ru'):String(y).localeCompare(x,'ru');
   });
   rows.forEach(r=>tb.appendChild(r));
  });
 });
}
document.querySelectorAll('table.s').forEach(sortable);
"""


def build(months: list[dict]) -> str:
    colors = ["#94A3B8", "#6366F1", "#10B981", "#F59E0B", "#EF4444"]
    for i, m in enumerate(months):
        m["color"] = colors[i % len(colors)]

    last = months[-1]
    prev = months[-2] if len(months) > 1 else None

    # общий список менеджеров: по нагрузке последнего месяца
    names: list[str] = []
    for m in months:
        for mg in m["managers"]:
            if mg["name"] not in names:
                names.append(mg["name"])
    order = {mg["name"]: mg["incidents"] for mg in last["managers"]}
    names.sort(key=lambda n: -order.get(n, 0))

    depts: list[str] = []
    for m in months:
        for d in m["depts"]:
            if d["dept"] not in depts:
                depts.append(d["dept"])

    data = {
        "months": [{"key": m["key"], "label": m["label"], "color": m["color"]} for m in months],
        "names": names,
        "depts": [{"key": d, "label": DEPT_RU.get(d, d)} for d in depts],
        "mgr": {m["key"]: {mg["name"]: mg for mg in m["managers"]} for m in months},
        "dept": {m["key"]: {d["dept"]: d for d in m["depts"]} for m in months},
    }

    def delta(cur, old, lower_is_better=False):
        if old in (None, 0) or cur is None:
            return '<div class="d flat">нет базы</div>'
        p = (cur - old) / old * 100
        good = (p < 0) if lower_is_better else (p > 0)
        cls = "up" if good else ("down" if abs(p) >= 1 else "flat")
        sign = "+" if p > 0 else ""
        return f'<div class="d {cls}">{sign}{p:.0f}% к пред. месяцу</div>'

    total_inc = sum(m["incidents"] for m in months)
    o = []
    o.append('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">')
    o.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    o.append("<title>Поддержка: сводный отчёт</title>")
    o.append(f"<style>{CSS}</style></head><body><div id=tip></div>")

    period = f'{months[0]["label"]} — {last["label"]}'
    o.append('<div class="hero"><div class="wrap">')
    o.append('<div class="brand"><b>MadeTask</b><span class="dot"></span>'
             '<span>Remozo</span><span class="dot"></span><span>Аналитика поддержки</span></div>')
    o.append("<h1>Работа поддержки по менеджерам</h1>")
    o.append(f'<div class="sub">{period}. Единица учёта это инцидент, а не диалог: переписка '
             f'режется на отдельные обращения по паузе в 24 часа. Источники ChatApp и Flomni, '
             f'методология E. Данные по {last["last_day"]}.</div>')
    o.append("</div></div>")

    o.append('<div class="wrap"><div class="kpis">')
    o.append(f'<div class="kpi"><div class="v">{total_inc}</div><div class="l">Инцидентов за период</div>'
             f'<div class="d flat">{len(months)} мес.</div></div>')
    o.append(f'<div class="kpi"><div class="v">{last["incidents"]}</div>'
             f'<div class="l">Инцидентов, {last["label"].split()[0].lower()}</div>'
             f'{delta(last["incidents"], prev["incidents"] if prev else None)}</div>')
    o.append(f'<div class="kpi"><div class="v">{fmt(last["frt_med_all"])}</div>'
             f'<div class="l">Медиана первого ответа</div>'
             f'{delta(last["frt_med_all"], prev["frt_med_all"] if prev else None, True)}</div>')
    o.append(f'<div class="kpi"><div class="v">{last["inc_pending"]}</div>'
             f'<div class="l">Ждут смежный отдел</div>'
             f'{delta(last["inc_pending"], prev["inc_pending"] if prev else None, True)}</div>')
    o.append("</div>")

    # ── 1. Нагрузка ──
    o.append('<h2><span class="num">1</span>Нагрузка по менеджерам</h2>')
    o.append('<p class="lead">Инцидент засчитывается каждому, кто написал в нём хотя бы один '
             'содержательный ответ. Если над обращением работали двое, оно попадёт обоим, '
             'поэтому столбец в сумме больше общего числа инцидентов.</p>')
    o.append('<div class="card">')
    o.append('<div class="toolbar"><div class="seg" id="segLoad">'
             '<button class="on" data-k="incidents">Инциденты</button>'
             '<button data-k="first">Первые ответы</button>'
             '<button data-k="messages">Сообщения</button>'
             '<button data-k="clients">Клиенты</button></div></div>')
    o.append('<div id="chLoad"></div><div class="legend" id="lgLoad"></div>')
    o.append("</div>")

    # ── 2. Скорость первого ответа ──
    o.append('<h2><span class="num">2</span>Скорость первого ответа</h2>')
    o.append('<p class="lead">Время от обращения клиента до первого содержательного ответа. '
             'Автоответы, приветствия и «мы получили ваш запрос» ответом не считаются. '
             'Метрика приписана тому, кто ответил первым.</p>')
    o.append('<div class="card">')
    o.append('<div class="toolbar"><div class="seg" id="segFrt">'
             '<button class="on" data-k="frt_med">Медиана</button>'
             '<button data-k="frt_avg">Среднее</button></div></div>')
    o.append('<div id="chFrt"></div><div class="legend" id="lgFrt"></div>')
    o.append('<div class="note"><b>Медиана и среднее расходятся в разы.</b> Медиана показывает '
             'типичный ответ, среднее чувствительно к нескольким долго провисевшим обращениям. '
             'Если среднее сильно выше медианы, у менеджера есть длинные хвосты.</div>')
    o.append("</div>")

    # ── 3. Таблица ──
    o.append('<h2><span class="num">3</span>Сводная таблица</h2>')
    o.append('<p class="lead"><span class="screen-only">Заголовки кликабельны, таблица '
             'сортируется. Переключатель меняет месяц.</span>'
             '<span class="print-only">Все три месяца подряд, внутри месяца по числу '
             'инцидентов.</span></p>')
    o.append('<div class="card">')
    o.append('<div class="toolbar"><div class="seg" id="segTbl">' + "".join(
        f'<button class="{"on" if i == len(months) - 1 else ""}" data-k="{m["key"]}">{m["label"]}</button>'
        for i, m in enumerate(months)) + "</div></div>")
    o.append('<table class="s" id="tblMgr"><thead><tr>'
             "<th>Менеджер</th><th>Инц</th><th>1-й отв</th><th>FRT медиана</th>"
             "<th>FRT среднее</th><th>Передач</th><th>Сообщ</th><th>Клиентов</th>"
             "</tr></thead><tbody></tbody></table>")
    o.append("</div>")

    # ── 4. Отделы ──
    o.append('<h2><span class="num">4</span>Смежные отделы: передачи и ожидание</h2>')
    o.append('<p class="lead">Передача открывается, когда поддержка пишет, что запрос ушёл в отдел, '
             'и закрывается ответом коллег или фактом решения. Колонки «всего / закрыто / открыто» '
             'считают сами передачи. Время ожидания считается только по закрытым: у открытой '
             'передачи нет момента ответа, и её длительность неизвестна. Поэтому отдел, из '
             'которого почти не возвращаются, показывает мало времени, но много открытых передач.</p>')
    o.append('<div class="card">')
    o.append('<div class="toolbar"><div class="seg" id="segDept">'
             '<button class="on" data-k="avg">Среднее ожидание</button>'
             '<button data-k="med">Медиана</button>'
             '<button data-k="total">Суммарно</button>'
             '<button data-k="n">Число передач</button>'
             '<button data-k="open">Открытые</button></div></div>')
    o.append('<div id="chDept"></div><div class="legend" id="lgDept"></div>')
    o.append("</div>")

    o.append('<div class="card" style="margin-top:14px">')
    o.append('<table class="s" id="tblDept"><thead><tr>'
             "<th>Отдел</th><th>Месяц</th><th>Передач</th><th>Закрыто</th><th>Открыто</th>"
             "<th>Среднее</th><th>Медиана</th><th>Дольше всего</th><th>Суммарно</th>"
             "</tr></thead><tbody></tbody></table>")
    o.append("</div>")

    # ── 5. SLA по сегментам ──
    o.append('<h2><span class="num">5</span>SLA по сегментам клиентов</h2>')
    o.append('<p class="lead">У каждого сегмента свой норматив на первый ответ и на решение. '
             'Сегмент заказчика определяется оборотом закрытых задач за предыдущий месяц: '
             'High больше 10 млн ₽, Medium от 3 до 10 млн ₽, Low меньше 3 млн ₽. Исполнители '
             'идут отдельным сегментом. Обе метрики считаются в рабочем времени поддержки '
             '(Пн–Пт, 10:00–19:00 МСК): норматив в 8 часов для Low в календарном времени '
             'смысла не имеет. Заказчики, у которых в данных нет компании, в этот раздел '
             'не попадают.</p>')

    def sla_pct(v):
        if v is None:
            return '<span class="muted">—</span>'
        cls = "up" if v >= 90 else ("flat" if v >= 70 else "down")
        return f'<b class="{cls}">{v:.0f}%</b>'

    o.append('<div class="card">')
    o.append('<table class="s"><thead><tr>'
             "<th>Сегмент</th><th>Месяц</th><th>Инц</th>"
             "<th>Норма 1-го ответа</th><th>Факт, медиана</th><th>В норме</th>"
             "<th>Норма решения</th><th>Факт, медиана</th><th>В норме</th>"
             "</tr></thead><tbody>")
    for seg_name in SEG_ORDER:
        for m in months:
            blk = m["sla"].get(seg_name)
            if not blk:
                continue
            tot = blk["mgr"].get("", {"frt": {"med": None, "ok": None},
                                      "ort": {"med": None, "ok": None}})
            o.append(f'<tr><td><b>{seg_name}</b></td><td>{m["label"]}</td>'
                     f'<td>{blk["inc"]}</td>'
                     f'<td class="muted">{fmt(blk["frt_norm"])}</td>'
                     f'<td>{fmt(tot["frt"]["med"])}</td><td>{sla_pct(tot["frt"]["ok"])}</td>'
                     f'<td class="muted">{fmt(blk["ort_norm"])}</td>'
                     f'<td>{fmt(tot["ort"]["med"])}</td><td>{sla_pct(tot["ort"]["ok"])}</td></tr>')
    o.append("</tbody></table></div>")

    o.append(f'<p class="lead" style="margin-top:18px">Разбивка по менеджерам за '
             f'{last["label"].lower()}. Первый ответ приписан тому, кто ответил первым, '
             f'решение тому, чьё сообщение закрыло обращение. Строки с одним-двумя '
             f'обращениями смотреть не стоит, там любая цифра случайна.</p>')
    for seg_name in SEG_ORDER:
        blk = last["sla"].get(seg_name)
        if not blk:
            continue
        o.append('<div class="card" style="margin-top:14px">')
        o.append(f'<div class="toolbar"><b>{seg_name}</b><span class="muted" '
                 f'style="margin-left:10px">норматив: первый ответ {fmt(blk["frt_norm"])}, '
                 f'решение {fmt(blk["ort_norm"])} рабочего времени</span></div>')
        o.append('<table class="s"><thead><tr><th>Менеджер</th>'
                 "<th>1-х отв</th><th>FRT медиана</th><th>FRT в норме</th>"
                 "<th>Решений</th><th>ORT медиана</th><th>ORT в норме</th>"
                 "</tr></thead><tbody>")
        ranked = sorted(((n, c) for n, c in blk["mgr"].items() if n),
                        key=lambda kv: -(kv[1]["frt"]["n"] + kv[1]["ort"]["n"]))
        for name, c in ranked:
            o.append(f'<tr><td>{name}</td>'
                     f'<td>{c["frt"]["n"]}</td><td>{fmt(c["frt"]["med"])}</td>'
                     f'<td>{sla_pct(c["frt"]["ok"])}</td>'
                     f'<td>{c["ort"]["n"]}</td><td>{fmt(c["ort"]["med"])}</td>'
                     f'<td>{sla_pct(c["ort"]["ok"])}</td></tr>')
        o.append("</tbody></table></div>")

    o.append('<div class="note"><b>Норматив жёстче там, где обслуживание не быстрее.</b> '
             'Фактическая скорость по сегментам почти не различается, а нормы отличаются '
             'в разы. Поэтому низкий процент у High это не столько провал людей, сколько '
             'отсутствие правила «крупный клиент идёт первым»: сейчас в очереди все равны.</div>')

    # ── 6. Повторные обращения ──
    o.append('<h2><span class="num">6</span>Повторные обращения</h2>')
    o.append('<p class="lead">Обращение закрыли, а человек вернулся с тем же вопросом. Этого не '
             'видят ни скорость первого ответа, ни отметка «решено»: быстрый ответ, который не '
             'помог, выглядит там как успех. Метрика показывает категории, где ломается процесс, '
             'а не человек, поэтому счёт идёт по темам, а не по менеджерам.</p>')
    incidents = rc.load_incidents()
    trend = []
    for m in months:
        y, mo = (int(x) for x in m["key"].split("-"))
        r = rc.compute(date(y, mo, 1), m["last_day"], incidents=incidents)
        trend.append({"label": m["label"], "pct": r["pct"], "closed": r["closed"]})
    y0, mo0 = (int(x) for x in months[0]["key"].split("-"))
    o.append(rc.render_html(
        rc.compute(date(y0, mo0, 1), last["last_day"], incidents=incidents), trend))

    # ── оговорки ──
    o.append('<h2><span class="num">7</span>Как читать эти цифры</h2>')
    o.append('<div class="card"><div class="note" style="margin-top:0">')
    o.append("<b>Ограничения, без которых выводы будут неверными.</b><ul>")
    jun = months[0]
    o.append(f'<li><b>{jun["label"]} по людям неполный.</b> В ChatApp тогда не записывалось поле '
             "с почтой оператора, поэтому работа сотрудников этого канала свалена в общую строку "
             "<code>RemozoSupport*</code>. Сравнивать людей между июнем и следующими месяцами нельзя, "
             "сравнимы только сотрудники Flomni и общие цифры по месяцу.</li>")
    o.append(f'<li><b>{last["label"]} неполный по датам.</b> Данные по {last["last_day"]}, '
             f'это {last["days"]} дней. Прямое сравнение с полным месяцем занижает показатели.</li>')
    o.append("<li><b>Инциденты общие.</b> В разделах 1, 2 и 3 обращение приписано каждому, кто в нём "
             "работал. Столбцы нельзя складывать между менеджерами. Раздел 4 свободен от этого: "
             "там счёт по инцидентам.</li>")
    o.append(f'<li><b>Часть работы без имени.</b> В {last["label"].lower()} {last["fr_unknown"]} первых '
             "ответов не привязаны к человеку. Это автоприветствия бота и общий аккаунт поддержки, "
             "за которым стоит живой человек, но имени в данных нет.</li>")
    o.append("<li><b>В отчёте только линия поддержки:</b> " + ", ".join(sorted(INCLUDE_OPS)) +
             f" и общий аккаунт <code>{SHARED_LABEL}</code>. Все остальные, кто писал клиентам, "
             "из выборки исключены: это сотрудники смежных команд и разовые заходы.</li>")
    o.append("<li><b>Время решения только в разделе 5.</b> Отметка «вопрос закрыт» ставится "
             "автоматически и иногда ошибается. В нагрузке и в сводной таблице её нет, чтобы "
             "не путать, а в SLA она нужна: без неё норматив на решение нечем измерить.</li>")
    o.append("<li><b>SLA считается в рабочем времени</b> поддержки: Пн–Пт, 10:00–19:00 МСК. "
             "Скорость первого ответа в разделе 2 наоборот календарная, поэтому цифры в "
             "разделах 2 и 5 не совпадают.</li>")
    o.append(f'<li><b>Без компании нет сегмента.</b> В {last["label"].lower()} '
             f'{last["sla_skipped"]} обращений заказчиков не удалось связать с компанией, они '
             f'в раздел 5 не вошли. Сегмент берётся по обороту за {last["seg_month"]}: он '
             "должен быть известен заранее, а не считаться задним числом.</li>")
    o.append("<li><b>Боты отсечены</b> по служебному флагу. В том числе аккаунт "
             "<code>ashumarin@fix.ru</code>, все сообщения которого автоматические.</li>")
    o.append("</ul></div></div>")

    o.append('<div class="foot">Источник: таблицы <code>dialogs</code>, <code>tickets</code>. '
             "Расчёт: <code>resolution_metrics.py</code>, тот же код, что и в ночной джобе. "
             f"Сборка: <code>build_manager_report.py</code>.</div>")
    o.append("</div>")

    o.append(f"<script>const D={json.dumps(data, ensure_ascii=False, default=str)};</script>")
    o.append("<script>" + JS + RENDER + "</script>")
    o.append("</body></html>")
    return "\n".join(o)


RENDER = r"""
function fmtDur(s){
 if(s==null)return '—';s=Math.round(s);
 if(s<60)return s+'с';
 if(s<3600)return Math.floor(s/60)+'м '+String(s%60).padStart(2,'0')+'с';
 if(s<86400)return Math.floor(s/3600)+'ч '+String(Math.floor(s%3600/60)).padStart(2,'0')+'м';
 return Math.floor(s/86400)+'д '+Math.floor(s%86400/3600)+'ч';}
function fmtNum(v){return Math.round(v).toLocaleString('ru');}

const state={load:'incidents',frt:'frt_med',dept:'avg',
 tbl:D.months[D.months.length-1].key,
 offLoad:new Set(),offFrt:new Set(),offDept:new Set()};

function legend(host,items,offSet,redraw){
 host.innerHTML='';
 items.forEach(it=>{
  const s=document.createElement('span');
  if(offSet.has(it.key))s.classList.add('off');
  s.innerHTML=`<i style="background:${it.color}"></i>${it.label}`;
  s.onclick=()=>{offSet.has(it.key)?offSet.delete(it.key):offSet.add(it.key);redraw();};
  host.appendChild(s);
 });
}

/* 1. нагрузка */
function drawLoad(){
 const k=state.load;
 const series=D.months.map(m=>({key:m.key,label:m.label,color:m.color,off:state.offLoad.has(m.key)}));
 const rows=D.names.map(n=>({label:n,vals:D.months.map(m=>{
   const g=D.mgr[m.key][n];const v=g?g[k]:0;
   return {series:m.key,value:v||0,
    tipHtml:`<b>${n}</b> · ${m.label}<br>Инцидентов: <b>${g?g.incidents:0}</b>`+
      `<br>Первых ответов: <b>${g?g.first:0}</b>`+
      `<br>Сообщений: <b>${g?g.messages:0}</b>`+
      `<br>Клиентов: <b>${g?g.clients:0}</b>`+
      `<br><span class="r">FRT медиана ${g?fmtDur(g.frt_med):'—'}</span>`};
 })}));
 groupedBar(document.getElementById('chLoad'),rows,series,fmtNum);
 legend(document.getElementById('lgLoad'),series,state.offLoad,drawLoad);
}

/* 2. FRT */
function drawFrt(){
 const k=state.frt;
 const series=D.months.map(m=>({key:m.key,label:m.label,color:m.color,off:state.offFrt.has(m.key)}));
 const rows=D.names.map(n=>({label:n,vals:D.months.map(m=>{
   const g=D.mgr[m.key][n];const v=g&&g[k]!=null?g[k]:0;
   return {series:m.key,value:v,
    tipHtml:`<b>${n}</b> · ${m.label}<br>Медиана: <b>${g?fmtDur(g.frt_med):'—'}</b>`+
      `<br>Среднее: <b>${g?fmtDur(g.frt_avg):'—'}</b>`+
      `<br><span class="r">по ${g?g.first:0} первым ответам</span>`};
 })})).filter(r=>r.vals.some(v=>v.value>0));
 groupedBar(document.getElementById('chFrt'),rows,series,fmtDur);
 legend(document.getElementById('lgFrt'),series,state.offFrt,drawFrt);
}

/* 3. таблица менеджеров */
function tblRows(mk){
 const list=Object.values(D.mgr[mk]).sort((a,b)=>b.incidents-a.incidents);
 const max=Math.max(...list.map(g=>g.incidents),1);
 return list.map(g=>`<tr>
  <td data-v="${g.name}">${g.name}</td>
  <td data-v="${g.incidents}">${g.incidents}<div class="mini" style="margin-top:4px"><i style="width:${g.incidents/max*100}%"></i></div></td>
  <td data-v="${g.first}">${g.first}</td>
  <td data-v="${g.frt_med??1e12}">${fmtDur(g.frt_med)}</td>
  <td data-v="${g.frt_avg??1e12}" class="muted">${fmtDur(g.frt_avg)}</td>
  <td data-v="${g.handoffs}">${g.handoffs}</td>
  <td data-v="${g.messages}">${g.messages}</td>
  <td data-v="${g.clients}">${g.clients}</td></tr>`).join('');
}

/* На экране показываем выбранный месяц, на печати все три подряд:
   переключатель в PDF не работает, а данные терять нельзя. */
function drawTbl(all){
 const tb=document.querySelector('#tblMgr tbody');
 if(!all){tb.innerHTML=tblRows(state.tbl);return;}
 tb.innerHTML=D.months.map(m=>
  `<tr class="grp"><td colspan="8">${m.label}</td></tr>`+tblRows(m.key)).join('');
}

/* 4. отделы */
function drawDept(){
 const k=state.dept;
 const series=D.months.map(m=>({key:m.key,label:m.label,color:m.color,off:state.offDept.has(m.key)}));
 const f=(k==='n'||k==='open')?fmtNum:fmtDur;
 const rows=D.depts.map(d=>({label:d.label,vals:D.months.map(m=>{
   const x=D.dept[m.key][d.key];
   return {series:m.key,value:(x&&x[k]!=null)?x[k]:0,
    tipHtml:`<b>${d.label}</b> · ${m.label}<br>Передач: <b>${x?x.n:0}</b>`+
      ` (закрыто ${x?x.closed:0}, открыто ${x?x.open:0})`+
      `<br>Среднее: <b>${x?fmtDur(x.avg):'—'}</b>`+
      `<br>Медиана: <b>${x?fmtDur(x.med):'—'}</b>`+
      `<br>Дольше всего: <b>${x?fmtDur(x.max):'—'}</b>`+
      `<br><span class="r">суммарно ${x?fmtDur(x.total):'—'} · по закрытым</span>`};
 })})).filter(r=>r.vals.some(v=>v.value>0));
 groupedBar(document.getElementById('chDept'),rows,series,f);
 legend(document.getElementById('lgDept'),series,state.offDept,drawDept);

 const tb=document.querySelector('#tblDept tbody');const out=[];
 D.depts.forEach(d=>D.months.forEach(m=>{
  const x=D.dept[m.key][d.key];if(!x)return;
  out.push(`<tr><td data-v="${d.label}">${d.label}</td>
   <td data-v="${m.key}" class="muted">${m.label}</td>
   <td data-v="${x.n}">${x.n}</td>
   <td data-v="${x.closed}">${x.closed||'—'}</td>
   <td data-v="${x.open}">${x.open||'—'}</td>
   <td data-v="${x.avg}">${fmtDur(x.avg)}</td>
   <td data-v="${x.med}">${fmtDur(x.med)}</td>
   <td data-v="${x.max}">${fmtDur(x.max)}</td>
   <td data-v="${x.total}">${fmtDur(x.total)}</td></tr>`);
 }));
 tb.innerHTML=out.join('');
}

function seg(id,fn){
 const box=document.getElementById(id);
 box.addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  box.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');fn(b.dataset.k);
 });
}
seg('segLoad',k=>{state.load=k;drawLoad();});
seg('segFrt',k=>{state.frt=k;drawFrt();});
seg('segTbl',k=>{state.tbl=k;drawTbl();});
seg('segDept',k=>{state.dept=k;drawDept();});

function drawAll(){drawLoad();drawFrt();drawTbl();drawDept();}
drawAll();
addEventListener('resize',()=>{drawLoad();drawFrt();drawDept();});
addEventListener('beforeprint',()=>{PRINT=true;drawTbl(true);drawLoad();drawFrt();drawDept();});
addEventListener('afterprint',()=>{PRINT=false;drawAll();});
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="+", required=True, help="YYYY-MM ...")
    ap.add_argument("-o", "--out", default="manager_report.html")
    args = ap.parse_args()

    months = []
    for spec in args.months:
        y, mo = (int(x) for x in spec.split("-"))
        print(f"считаю {spec} ...", flush=True)
        m = collect(y, mo)
        print(f"  инцидентов={m['incidents']} менеджеров={len(m['managers'])} "
              f"отделов={len(m['depts'])} последний день={m['last_day']}")
        months.append(m)

    html = build(months)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"готово: {args.out} ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
