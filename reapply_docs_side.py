"""
Разовый фикс стороны для диалогов, затронутых docs-backfill.

Причина: backfill_docs_subcategory слепо доверял полю `side` от LLM, а новый
промпт массово выдавал customer — даже когда сам LLM выбирал executor-категорию
«Запрос документов». В результате часть исполнителей ошибочно ушла в customer.

Правило переприменения (без новых вызовов LLM, из /tmp/docs_subcat_cache.json):
  1. групповой кабинет -> customer (как раньше);
  2. иначе если LLM выбрал executor-категорию «Запрос документов» -> executor;
  3. иначе доверяем LLM side (с fallback на текущий side строки).

Обновляет последнюю AnalysisResult каждого диалога (side/category/subcategory).

Запуск:
  python reapply_docs_side.py [--apply]   (по умолчанию dry-run)
"""

import json
import logging
import sys
from collections import Counter

from sqlalchemy import text as sqlt

import ai_analysis as ai
from database import AnalysisResult, get_session

log = logging.getLogger("reapply_docs_side")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

_CACHE_PATH = "/tmp/docs_subcat_cache.json"
EXECUTOR_DOC_CATEGORY = "Запрос документов"


def run(apply: bool) -> None:
    with open(_CACHE_PATH, encoding="utf-8") as f:
        cache = json.load(f)

    dialog_ids = [int(k) for k in cache.keys()]

    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, d.client_id,
                   ar.side AS cur_side, ar.category AS cur_cat
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.id = ANY(:ids)
        """), {"ids": dialog_ids}).fetchall()
        group_clients = ai.group_cabinet_ids(db)

    log.info("Loaded %d dialogs (%d group cabinets).", len(rows), len(group_clients))

    flips = Counter()
    dist_side = Counter()
    changed = 0

    for r in rows:
        result = cache.get(str(r.dialog_id))
        if result is None:
            continue

        llm_cat = (result.get("category") or "").strip()

        if r.client_id in group_clients:
            side = "customer"
        elif llm_cat == EXECUTOR_DOC_CATEGORY:
            side = "executor"
        else:
            side = (result.get("side") or "").strip().lower()
            if side not in ("customer", "executor"):
                side = r.cur_side or "executor"

        category = ai._validate_category(side, llm_cat)
        subcategory = ai._validate_subcategory(category, result.get("subcategory"))

        dist_side[side] += 1
        if side != r.cur_side or category != r.cur_cat:
            flips[(r.cur_side, side)] += 1
            changed += 1

        if apply and (side != r.cur_side or category != r.cur_cat):
            with get_session() as db:
                obj = db.get(AnalysisResult, r.ar_id)
                obj.side = side
                obj.category = category
                obj.subcategory = subcategory
                db.commit()

    log.info("=== new side distribution ===")
    for s, n in dist_side.most_common():
        log.info("  %-10s %d", s, n)
    log.info("=== transitions (old_side -> new_side) ===")
    for (a, b), n in flips.most_common():
        log.info("  %-10s -> %-10s %d", a, b, n)
    log.info("Done. apply=%s changed=%d / %d", apply, changed, len(rows))


if __name__ == "__main__":
    run("--apply" in sys.argv)
