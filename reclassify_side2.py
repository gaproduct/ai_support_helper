"""
Side-only re-classification pass #2 — dedicated few-shot side classifier.

Targets the systematic error where first-person executor messages
("я не могу пройти KYC", "не пришла моя выплата", "change my email") were
classified as `customer`. Re-checks customer-side dialogs in a date range with
a tight side-only prompt (few-shot, real examples) and flips to `executor`
where warranted. On flip, category is remapped via ai_analysis.CROSS_SIDE_MAP.

Categories "Тест" and empty dialogs are skipped. Only the latest AnalysisResult
per dialog is updated. Does not touch dialogs already on the executor side.
"""

import json
import logging
import sys

from openai import OpenAI
from sqlalchemy import text as sqlt

import ai_analysis as ai
from config import settings
from database import AnalysisResult, get_session

log = logging.getLogger("reclassify_side2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

client = OpenAI(api_key=settings.openai_api_key)

SIDE_PROMPT = """\
Ты определяешь СТОРОНУ автора обращения в поддержку B2B-платёжного сервиса.
Верни ТОЛЬКО валидный JSON: {"side": "customer"} или {"side": "executor"}.

executor = ИСПОЛНИТЕЛЬ / КОНТРАКТОР (физлицо или ИП, которое ПОЛУЧАЕТ выплаты).
  Пишет от ПЕРВОГО ЛИЦА про СВОЁ: свою выплату, свой KYC, свой аккаунт/ЛК,
  свой налоговый статус/ИП/самозанятость, свой email/телефон, свою регистрацию.

customer = ЗАКАЗЧИК-ПЛАТЕЛЬЩИК (компания / ИП, которая ПЛАТИТ исполнителям).
  Пишет про ДРУГОГО человека: «верифицируйте нашего сотрудника», «выплата
  пользователю X@mail», называет ФИО / email / id ТРЕТЬЕГО лица, говорит от
  лица бизнеса «у нас», «наша компания», «наши исполнители».

ГЛАВНОЕ ПРАВИЛО: первое лицо про СВОЁ → executor (даже если это «клиент сервиса»).
customer — ТОЛЬКО тот, кто ПЛАТИТ исполнителям и пишет про чужие выплаты/верификацию.
Префикс «@username Имя:» — это просто автор реплики, он НЕ означает customer.

ПРИМЕРЫ:
"Я в том же регионе, где и был, деньги выводиться перестали" → {"side": "executor"}
"мне нужно изменить налоговый статус на ИП" → {"side": "executor"}
"я пыталась пройти верификацию, мне пишет что не могу из-за региона" → {"side": "executor"}
"how I can change my email address, old domain was blocked" → {"side": "executor"}
"I need to confirm my tax status as individual entrepreneur in Russia" → {"side": "executor"}
"Я зарегистрировался как подрядчик и хочу получать выплаты на ИП" → {"side": "executor"}
"не могу зайти в ЛК, пароль не меняла" → {"side": "executor"}
"Я открыл аккаунт, но ещё нет денег" → {"side": "executor"}
"не получается подтвердить статус самозанятого" → {"side": "executor"}
"Вот подтверждение закрытия ИП" → {"side": "executor"}
"помогите верифицировать нашего сотрудника al.shinshinov@mail.ru" → {"side": "customer"}
"пользователь vatueshar@gmail.com вернул по ошибке платеж в банк" → {"side": "customer"}
"данные для исполнителя arinapavlenko@gmail.com: Ms. Arina Pavlenko" → {"side": "customer"}
"Уточните, почему пользователь не может вывести средства?" → {"side": "customer"}
"Архив со всеми документами по вчерашним выплатам на ИП РФ прилагаю" → {"side": "customer"}
"АНДРЕЙ ПОТАПОВ СЕРГЕЕВИЧ почему отменена выплата? человеку по этим же реквизитам уже проходило" → {"side": "customer"}
"Гришанов Дмитрий Александрович, gda@ruswater.com, верификация прошла некорректно" → {"side": "customer"}

ВАЖНО: если назван КОНКРЕТНЫЙ человек по ФИО или указан чужой email и вопрос
про ЕГО выплату/верификацию — это customer (заказчик пишет про своего исполнителя),
даже если фраза звучит как «почему отменена выплата».

Не добавляй текста вне JSON.
"""


def _inbound_only(messages_text: str) -> str:
    """Return only inbound (client) message texts, newline-joined.

    Operator replies ("укажите email исполнителя") add B2B-sounding noise that
    biases the side classifier toward customer, so we strip them out.
    """
    try:
        msgs = json.loads(messages_text)
    except (json.JSONDecodeError, TypeError):
        return messages_text
    if not isinstance(msgs, list):
        return messages_text
    lines = [
        (m.get("text") or "").strip()
        for m in msgs
        if m.get("direction") == "inbound" and (m.get("text") or "").strip()
    ]
    return "\n".join(lines)


def _classify_side(dialog_text: str) -> str | None:
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SIDE_PROMPT},
                {"role": "user", "content": dialog_text},
            ],
            max_tokens=20,
            temperature=0,
            response_format={"type": "json_object"},
        )
        side = (json.loads(resp.choices[0].message.content or "{}").get("side") or "").strip().lower()
        return side if side in ("customer", "executor") else None
    except Exception as exc:
        log.error("side classify error: %s", exc)
        return None


def run(date_from: str, date_to: str) -> None:
    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, ar.category AS old_cat,
                   d.messages_text
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
              AND ar.side = 'customer'
              AND ar.category <> 'Тест'
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to}).fetchall()

    log.info("Loaded %d customer dialogs to re-check.", len(rows))
    flips = 0
    for i, r in enumerate(rows, 1):
        text = _inbound_only(r.messages_text or "")
        if not text.strip():
            continue
        new_side = _classify_side(text)
        if new_side != "executor":
            continue
        new_cat = ai._validate_category("executor", r.old_cat)
        with get_session() as db:
            obj = db.get(AnalysisResult, r.ar_id)
            obj.side = "executor"
            obj.category = new_cat
            db.commit()
        flips += 1
        log.info("[%d/%d] dialog %d: customer->executor, cat %s->%s",
                 i, len(rows), r.dialog_id, r.old_cat, new_cat)
        if i % 50 == 0:
            log.info("progress %d/%d flips=%d", i, len(rows), flips)

    log.info("Done. flips=%d / %d customer dialogs.", flips, len(rows))


if __name__ == "__main__":
    df = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
    dt = sys.argv[2] if len(sys.argv) > 2 else "2026-06-25"
    run(df, dt)
