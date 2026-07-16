"""
backfill_reduce_drugoe.py — точечный ре-прогон LLM по диалогам «Другое».

Берёт диалоги, у которых последний AnalysisResult сейчас в category='Другое',
и прогоняет ТЕКУЩИЙ классификатор (ai_analysis с усиленным §6 против «Другого»).
Если модель уверенно даёт тематическую категорию — обновляет строку
(side / category / subcategory). Диалоги, оставшиеся «Другое», не трогает.

Запускать ПОСЛЕ reconcile_drugoe.py (тот дешёво чинит raw!='Другое').

Кэш LLM: /tmp/reduce_drugoe_cache.json (temperature=0 → детерминированно).

Запуск:
  python backfill_reduce_drugoe.py [date_from] [date_to] [--apply]
По умолчанию dry-run.
"""

import json
import logging
import os
import sys
from collections import Counter

from sqlalchemy import text as sqlt

import ai_analysis as ai
from database import AnalysisResult, get_session

log = logging.getLogger("backfill_reduce_drugoe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

_CACHE_PATH = "/tmp/reduce_drugoe_cache.json"


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
                   d.messages_text, ar.side AS old_side
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
              AND ar.category = 'Другое'
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to}).fetchall()

        group_clients = ai.group_cabinet_ids(db)

    log.info("Loaded %d 'Другое' dialogs (%d group cabinets).", len(rows), len(group_clients))

    cache = _load_cache()
    dist: Counter = Counter()
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

        dist[category] += 1
        if category != "Другое":
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
    log.info("=== New category distribution for former 'Другое' ===")
    for cat, n in dist.most_common():
        log.info("  %-45s %d", cat, n)
    log.info("moved out of 'Другое': %d", moved_out)
    log.info("Done. apply=%s updated=%d / %d", apply, updated, len(rows))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    df = args[0] if len(args) > 0 else "2026-06-01"
    dt = args[1] if len(args) > 1 else "2026-06-30"
    run(df, dt, apply)
