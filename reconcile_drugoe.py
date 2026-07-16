"""
reconcile_drugoe.py — дешёвая (без LLM) починка «Другое».

Часть диалогов стоит в category='Другое', хотя в raw_response лежит валидная
категория (её переписал прошлый reclassify_side-проход, не применив CROSS_SIDE_MAP,
либо категория-alias успела устареть). Скрипт берёт последний AnalysisResult
каждого диалога с category='Другое', вытаскивает category/subcategory из
raw_response и прогоняет через ТЕКУЩИЙ ai._validate_category(<stored side>, …).
Если получается не «Другое» — обновляет строку (category + subcategory).

Сторона (side) НЕ меняется — уважаем решения reclassify_side / групп-кабинета.

Запуск:
  python reconcile_drugoe.py [date_from] [date_to] [--apply]
По умолчанию dry-run.
"""

import json
import logging
import sys
from collections import Counter

from sqlalchemy import text as sqlt

import ai_analysis as ai
from database import AnalysisResult, get_session

log = logging.getLogger("reconcile_drugoe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def run(date_from: str, date_to: str, apply: bool) -> None:
    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, ar.side AS side,
                   ar.raw_response AS raw
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
              AND ar.category = 'Другое'
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to}).fetchall()

    log.info("Loaded %d 'Другое' dialogs.", len(rows))

    dist: Counter = Counter()
    fixed = 0
    for r in rows:
        try:
            obj = json.loads(r.raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        raw_cat = (obj.get("category") or "").strip()
        if not raw_cat or raw_cat == "Другое":
            continue

        side = r.side or "executor"
        new_cat = ai._validate_category(side, raw_cat)
        if new_cat == "Другое":
            continue

        new_sub = ai._validate_subcategory(new_cat, obj.get("subcategory"))
        dist[f"{raw_cat} → {new_cat}"] += 1
        fixed += 1

        if apply:
            with get_session() as db:
                a = db.get(AnalysisResult, r.ar_id)
                a.category = new_cat
                a.subcategory = new_sub
                db.commit()

    log.info("=== Reconciled mappings ===")
    for k, n in dist.most_common():
        log.info("  %-60s %d", k, n)
    log.info("Done. apply=%s fixed=%d / %d", apply, fixed, len(rows))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    df = args[0] if len(args) > 0 else "2026-06-01"
    dt = args[1] if len(args) > 1 else "2026-06-30"
    run(df, dt, apply)
