"""
Сторона диалога по фактам, а не по тексту.

AI-классификатор путает заказчика и исполнителя примерно в каждом пятом
случае и размечает один и тот же чат в разные стороны в разных диалогах
(243 чата с разнобоем). Здесь сторона чинится двумя фактами:

  R1 «группа»      групповой чат — это всегда координация с заказчиком:
                   отрицательный telegram id в chatapp, бренд в названии
                   (MadeTask / Remozo) или «&» между компаниями.
                   Исключение: ленты кошелька («Wallet ... transactions»),
                   это бот-уведомления, а не клиент. Их не трогаем.
  R2 «большинство» один чат = один клиент = одна сторона. Если по чату
                   два и больше диалогов и есть строгое большинство,
                   меньшинство перекрашивается в сторону большинства.

R1 сильнее R2. Чаты без фактов не трогаем. При смене стороны категория
переводится через ai_analysis._validate_category (CROSS_SIDE_MAP внутри).
Обновляется только последний AnalysisResult диалога, как в прошлых
переразметках (reclassify_side2_strict).

CLI:
  python reclassify_side_facts.py            # dry-run, только отчёт
  python reclassify_side_facts.py --apply    # записать в БД
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from sqlalchemy import text

import ai_analysis as ai
from database import engine

log = logging.getLogger("reclassify_side_facts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

FACTS_SQL = """
WITH last_ar AS (
  SELECT DISTINCT ON (ar.dialog_id) ar.id ar_id, ar.dialog_id, ar.side, ar.category
  FROM analysis_results ar ORDER BY ar.dialog_id, ar.created_at DESC),
d2 AS (
  SELECT d.id, d.source, d.client_id, la.ar_id, la.side, la.category,
    (d.chat_name ~* 'wallet' AND d.chat_name ~* 'transaction') AS is_bot_feed,
    ((d.source = 'chatapp' AND d.client_id LIKE '-%')
     OR d.chat_name ~* 'madetask|made-task|remozo'
     OR d.chat_name LIKE '%&%') AS is_group
  FROM dialogs d JOIN last_ar la ON la.dialog_id = d.id
  WHERE NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s WHERE s.dialog_id = d.id)
    AND la.side IN ('executor', 'customer')),
chat_fact AS (
  SELECT source, client_id,
    BOOL_OR(is_group) grp, BOOL_OR(is_bot_feed) bot,
    COUNT(*) n,
    COUNT(*) FILTER (WHERE side = 'executor') ne,
    COUNT(*) FILTER (WHERE side = 'customer') nc
  FROM d2 GROUP BY 1, 2),
fact AS (
  SELECT source, client_id,
    CASE WHEN bot THEN NULL
         WHEN grp THEN 'customer'
         WHEN n >= 2 AND ne > nc THEN 'executor'
         WHEN n >= 2 AND nc > ne THEN 'customer'
         ELSE NULL END fact_side,
    CASE WHEN bot THEN NULL WHEN grp THEN 'группа' ELSE 'большинство' END rule
  FROM chat_fact)
SELECT d2.id dialog_id, d2.ar_id, d2.side, d2.category, f.fact_side, f.rule
FROM d2 JOIN fact f USING (source, client_id)
WHERE f.fact_side IS NOT NULL AND f.fact_side <> d2.side
ORDER BY d2.id
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать в БД")
    args = parser.parse_args()

    with engine.connect() as conn:
        rows = conn.execute(text(FACTS_SQL)).fetchall()

    by_rule = Counter()
    cat_moves = Counter()
    updates = []
    for r in rows:
        new_cat = ai._validate_category(r.fact_side, r.category or "")
        by_rule[(r.rule, f"{r.side} -> {r.fact_side}")] += 1
        if new_cat != r.category:
            cat_moves[f"{r.category} -> {new_cat}"] += 1
        updates.append({"ar_id": r.ar_id, "side": r.fact_side, "cat": new_cat})

    log.info("диалогов к перевороту: %d", len(rows))
    for (rule, flip), n in by_rule.most_common():
        log.info("  %-12s %-24s %d", rule, flip, n)
    if cat_moves:
        log.info("категории вслед за стороной:")
        for move, n in cat_moves.most_common():
            log.info("  %3d  %s", n, move)

    if not args.apply:
        log.info("dry-run, БД не тронута. Запусти с --apply для записи.")
        return

    with engine.begin() as conn:
        for u in updates:
            conn.execute(text(
                "UPDATE analysis_results SET side = :side, category = :cat "
                "WHERE id = :ar_id"
            ), u)
    log.info("записано обновлений: %d", len(updates))
    log.info("дальше: python compute_tickets.py --date-from ... --date-to ..., "
             "чтобы tickets подхватили новую сторону и категорию.")


if __name__ == "__main__":
    main()
