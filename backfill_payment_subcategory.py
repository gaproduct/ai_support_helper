"""
Backfill подкатегорий для категории «Выплаты и проблемы с ними».

Прогоняет ТЕКУЩИЙ классификатор (ai_analysis с новым промптом: subcategory +
разведение «Зачисления на баланс») по диалогам, у которых последний
AnalysisResult сейчас в категории «Выплаты и проблемы с ними», и ОБНОВЛЯЕТ
эту же строку:
  - side       — групповой кабинет → customer (как в основном пайплайне),
                 иначе из ответа модели;
  - category   — может смениться (например, на «Зачисление платежа на баланс»,
                 если кейс про входящее пополнение, а не исходящую выплату);
  - subcategory— одна из подкатегорий «Выплат», либо NULL после ухода из «Выплат».

Идемпотентно по содержанию: повторный прогон даёт тот же результат.

Запуск:
  python backfill_payment_subcategory.py [date_from] [date_to] [--apply]
По умолчанию dry-run (только распределение, без UPDATE).
"""

import json
import logging
import os
import sys
from collections import Counter

from sqlalchemy import text as sqlt

import ai_analysis as ai
from database import AnalysisResult, get_session

log = logging.getLogger("backfill_payment_subcategory")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

TARGET_CATEGORY = "Выплаты и проблемы с ними"
# Кэш сырых LLM-ответов по dialog_id, чтобы --apply не звал модель повторно
# после dry-run (одни и те же диалоги, детерминированный temperature=0).
_CACHE_PATH = "/tmp/payment_subcat_cache.json"


def _load_cache() -> dict:
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def run(date_from: str, date_to: str, apply: bool) -> None:
    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, d.client_id,
                   d.messages_text, ar.side AS old_side, ar.category AS old_cat
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
              AND ar.category = :cat
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to, "cat": TARGET_CATEGORY}).fetchall()

        group_clients = ai.group_cabinet_ids(db)

    log.info("Loaded %d '%s' dialogs (%d group cabinets).",
             len(rows), TARGET_CATEGORY, len(group_clients))

    cache = _load_cache()
    dist_cat: Counter = Counter()
    dist_sub: Counter = Counter()
    moved_out = 0
    updated = 0

    for i, r in enumerate(rows, 1):
        if not r.messages_text or not r.messages_text.strip():
            continue

        key = str(r.dialog_id)
        result = cache.get(key)
        if result is None:
            result, _ = ai._call_openai(ai._messages_to_prompt(r.messages_text))
            if result is None:
                log.warning("[%d/%d] dialog %d: no LLM result, skipping.", i, len(rows), r.dialog_id)
                continue
            cache[key] = result
            if i % 25 == 0:
                _save_cache(cache)

        if r.client_id in group_clients:
            side = "customer"
        else:
            side = (result.get("side") or "").strip().lower()
            if side not in ("customer", "executor"):
                side = r.old_side or "executor"

        category = ai._validate_category(side, (result.get("category") or "").strip())
        subcategory = ai._validate_subcategory(category, result.get("subcategory"))

        dist_cat[category] += 1
        dist_sub[subcategory or "(нет)"] += 1
        if category != TARGET_CATEGORY:
            moved_out += 1

        if apply:
            with get_session() as db:
                obj = db.get(AnalysisResult, r.ar_id)
                obj.side = side
                obj.category = category
                obj.subcategory = subcategory
                db.commit()
            updated += 1

        if i % 50 == 0:
            log.info("progress %d/%d (moved_out=%d)", i, len(rows), moved_out)

    _save_cache(cache)
    log.info("=== Category distribution after reclassification ===")
    for cat, n in dist_cat.most_common():
        log.info("  %-45s %d", cat, n)
    log.info("=== Subcategory distribution (within '%s' stays) ===", TARGET_CATEGORY)
    for sub, n in dist_sub.most_common():
        log.info("  %-45s %d", sub, n)
    log.info("moved out of '%s': %d", TARGET_CATEGORY, moved_out)
    log.info("Done. apply=%s updated=%d / %d", apply, updated, len(rows))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    df = args[0] if len(args) > 0 else "2026-06-01"
    dt = args[1] if len(args) > 1 else "2026-06-25"
    run(df, dt, apply)
