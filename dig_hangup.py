"""Detail the subcategory 'Выплаты → Зависание в обработке' (July):
cluster each dialog into a finer bucket describing WHAT is stuck and WHY,
by scanning message text + summary + rationale."""
import json
import re
from collections import Counter, defaultdict

from database import get_session
from sqlalchemy import text as t

CAT = "Выплаты и проблемы с ними"
SUB = "Зависание в обработке"

# Ordered: first match wins (specific → generic).
BUCKETS = [
    ("Зависла проверка комплаенс/безопасности", [
        r"комплаенс", r"complianc", r"служба безопасн", r"проверк.{0,15}безопасн",
        r"на проверк.{0,15}(комплаенс|безопасн|риск)", r"финмониторинг",
    ]),
    ("Зависла из-за верификации/KYC", [
        r"верификац", r"kyc", r"не верифиц", r"пройдите.{0,20}(кус|верифик)",
        r"пока.{0,15}не пройд.{0,15}верифик", r"подожду разблок", r"жду разблок",
    ]),
    ("Технический сбой/ошибка обработки", [
        r"технич.{0,15}(сбо|ошибк|проблем)", r"ошибка.{0,15}(обработ|систем|операц)",
        r"систем.{0,15}(сбо|не дает|не работ|глюч|виснет)", r"error", r"сбой",
        r"падает с ошибк", r"с ошибк", r"failed",
    ]),
    ("Массовое/реестровое зависание (много выплат)", [
        r"реестр", r"пакет.{0,10}выплат", r"все выплат.{0,15}(завис|в обработ|не прош)",
        r"много выплат", r"массов.{0,15}(выплат|зависан)", r"несколько выплат.{0,15}завис",
        r"пачк.{0,10}выплат",
    ]),
    ("Пополнение/депозит: зачисление на баланс платформы (заказчик)", [
        r"пополн", r"депозит", r"\bdeposit\b", r"перевод на пополнение",
        r"зачислен.{0,15}(на баланс|на платформ|на счет)", r"funds received",
        r"проконтролируйте.{0,10}зачисл", r"проверьте.{0,10}(зачисл|депозит|поступлен)",
        r"как скоро.{0,15}зачисл", r"ускорить зачислен", r"средства.{0,10}зачислен",
        r"successfully transferred to your balance", r"сделали платеж.{0,15}usdt",
        r"ожидаем пополнен",
    ]),
    ("Вывод в криптовалюте (завис/недоступен)", [
        r"крипт", r"crypto", r"usdt.{0,15}(вывод|кошел)", r"криптокошел",
        r"send money to.{0,10}crypto", r"вывод.{0,15}крипт", r"крипт.{0,15}(вывод|кошел|не работ|нет)",
    ]),
    ("Выплата отправлена в банк, ждём зачисления", [
        r"направил.{0,15}(запрос|выплат).{0,20}банк", r"ожидайте.{0,20}поступл",
        r"в течение.{0,10}\d.{0,10}(рабоч|дн)", r"\brrn\b",
        r"успешно проведен.{0,25}(ожид|не пришл|банк)",
        r"на стороне банка", r"банк.{0,15}зачисл", r"платеж отправлен",
    ]),
    ("Запрошен вывод — статус не меняется", [
        r"запрос.{0,15}вывод.{0,25}(не мен|висит|завис|нет)",
        r"запросил.{0,15}вывод", r"подал.{0,15}(на вывод|заявк)",
        r"вывод.{0,15}(не мен|висит|завис|в обработ)", r"статус.{0,15}не мен",
    ]),
    ("Долго в статусе «в обработке» (процессинг)", [
        r"в обработк", r"обрабат.{0,15}(долго|уже|несколько|давно)",
        r"долго.{0,15}(обрабат|в обработ|идет|висит)", r"статус.{0,15}(в обработ|обрабат|processing)",
        r"processing", r"in progress", r"in_progress", r"висит.{0,15}(в обработ|выплат)",
        r"завис.{0,15}(в обработ|обработ)", r"stuck",
    ]),
    ("Задержка транзакции (обычно быстрее)", [
        r"задержк.{0,15}(транзакц|выплат|платеж|процесс)", r"обычно.{0,15}(быстр|мгновен)",
        r"дольше обычного", r"почему.{0,15}(долго|задерж)", r"деньги доходили быстрее",
        r"чтобы деньги доходили быстрее",
    ]),
    ("Расхождение баланса в ЛК (0/не отображается)", [
        r"баланс.{0,15}(0|ноль|не отобра|не измен|пуст)", r"в кабинете.{0,10}(0|ноль|по 0)",
        r"показывает.{0,15}(0|ноль|получил)", r"по 0 у него", r"на балансе ничего",
    ]),
    ("Выплата долго не приходит / где деньги", [
        r"не приход.{0,15}(выплат|деньг|перевод)", r"сутки.{0,15}не приход",
        r"давно.{0,15}не приход", r"где.{0,10}(деньги|выплат|перевод)",
        r"когда.{0,15}(придут|поступ|зачисл|выплат)", r"деньги.{0,10}не пришл",
        r"не поступил", r"долго.{0,15}не приход", r"не дошл", r"деньги не дошл",
    ]),
    ("Уточнение статуса выплаты (общий)", [
        r"статус.{0,15}(выплат|платеж|перевод|операц)", r"уточнит.{0,15}статус",
        r"прошла ли выплат", r"проверьте.{0,15}(выплат|статус|платеж)",
        r"что со.{0,10}(выплат|платеж)", r"как.{0,10}там.{0,10}выплат",
        r"было ли поступлен", r"проверить.{0,15}(транзакц|платеж|выплат|поступлен)",
        r"сумму выплаты", r"где.{0,15}сколько ожидать", r"скоро.{0,10}зачисл",
    ]),
]

COMPILED = [(name, [re.compile(p, re.I) for p in pats]) for name, pats in BUCKETS]


def classify(text: str) -> str:
    for name, pats in COMPILED:
        for p in pats:
            if p.search(text):
                return name
    return "НЕ РАСПОЗНАНО"


def main():
    s = get_session()
    sql = f"""
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, subcategory, summary, rationale, side
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT tk.dialog_id, tk.side, tk.count, coalesce(tk.company,''),
           d.messages_json, ar.summary, ar.rationale
    FROM tickets tk
    JOIN ar ON ar.dialog_id=tk.dialog_id
    JOIN dialogs d ON d.id=tk.dialog_id
    WHERE tk.dialog_date>='2026-07-01' AND tk.dialog_date<='2026-07-31' AND tk.methodology='E'
      AND tk.category='{CAT}' AND ar.subcategory='{SUB}'
    """
    rows = s.execute(t(sql)).fetchall()

    bucket_rows = Counter()
    bucket_tickets = Counter()
    bucket_side = defaultdict(Counter)
    unknown = []

    for did, side, cnt, company, mj, summary, rationale in rows:
        try:
            msgs = json.loads(mj or "[]")
        except Exception:
            msgs = []
        blob = " ".join(m.get("text", "") for m in msgs)
        blob += " " + (summary or "") + " " + (rationale or "")
        b = classify(blob)
        bucket_rows[b] += 1
        bucket_tickets[b] += cnt
        bucket_side[b][side] += 1
        if b == "НЕ РАСПОЗНАНО":
            unknown.append((did, side, (rationale or summary or "")[:160]))

    total = sum(bucket_rows.values())
    print(f"Всего диалогов: {total}; тикетов: {sum(bucket_tickets.values())}\n")
    print(f"{'тикетов':>7} {'диал.':>6} {'cust':>5} {'exec':>5}  подкатегория (что зависло)")
    print("-" * 78)
    for name, _ in BUCKETS + [("НЕ РАСПОЗНАНО", [])]:
        if bucket_rows[name] == 0:
            continue
        print(f"{bucket_tickets[name]:7d} {bucket_rows[name]:6d} "
              f"{bucket_side[name]['customer']:5d} {bucket_side[name]['executor']:5d}  {name}")

    print(f"\n=== НЕ РАСПОЗНАНО: {len(unknown)} (нужен ручной разбор) ===")
    for did, side, txt in unknown[:40]:
        print(f"  [{did} {side}] {txt}")


if __name__ == "__main__":
    main()
