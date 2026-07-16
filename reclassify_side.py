"""
Side-only re-classification (вариант 1).

Перепрогоняет диалоги за период новым промптом и обновляет ТОЛЬКО `side`.
При флипе стороны категория переносится на корректную сторону через
ai._validate_category (CROSS_SIDE_MAP), сохраняя тему ручных правок.

Категория «Тест» не трогается — это маркер-исключение из тикетов.
Обновляется последняя (MAX id) AnalysisResult каждого диалога — та же,
что читает compute_tickets.py.

CLI: python3 reclassify_side.py [date_from] [date_to]
"""

import logging
import sys

from sqlalchemy import text as sqlt

import ai_analysis as ai
from database import AnalysisResult, get_session


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("reclassify_side")

PROTECT_CATEGORIES = frozenset({"Тест"})


def run(date_from: str, date_to: str) -> None:
    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, ar.side AS old_side,
                   ar.category AS old_category, d.messages_text
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) AS mx
                  FROM analysis_results GROUP BY dialog_id) latest
                 ON latest.dialog_id = ar.dialog_id AND latest.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to}).fetchall()

    total = len(rows)
    log.info("Loaded %d dialogs to re-check side (%s..%s).", total, date_from, date_to)

    flips = 0
    errors = 0
    for i, r in enumerate(rows, 1):
        if r.old_category in PROTECT_CATEGORIES:
            continue
        if not (r.messages_text or "").strip():
            continue

        result, _ = ai._call_openai(ai._messages_to_prompt(r.messages_text))
        if not result:
            errors += 1
            log.warning("dialog %d: no LLM result, skip", r.dialog_id)
            continue

        new_side = (result.get("side") or "").strip().lower()
        if new_side not in ("customer", "executor"):
            continue
        if new_side == r.old_side:
            continue

        new_category = ai._validate_category(new_side, r.old_category)

        with get_session() as db:
            ar_obj = db.get(AnalysisResult, r.ar_id)
            ar_obj.side = new_side
            ar_obj.category = new_category
            db.commit()

        flips += 1
        log.info("[%d/%d] dialog %d: side %s->%s | cat %s->%s",
                 i, total, r.dialog_id, r.old_side, new_side,
                 r.old_category, new_category)

        if i % 50 == 0:
            log.info("--- progress %d/%d, flips=%d, errors=%d ---", i, total, flips, errors)

    log.info("Done. flips=%d  errors=%d  total=%d", flips, errors, total)


if __name__ == "__main__":
    df = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
    dt = sys.argv[2] if len(sys.argv) > 2 else "2026-06-25"
    run(df, dt)
