"""Классифицирует диалоги двух крупных подкатегорий выплат в детальные
fine-buckets и пишет в таблицу dialog_fine_subcategory (одна строка = один
диалог). Идемпотентно (upsert по dialog_id). Источник правил — fine_subcategory.

CLI:
  python compute_fine_subcategory.py            # все диалоги этих подкатегорий
  python compute_fine_subcategory.py --date-from 2026-07-01 --date-to 2026-07-31
"""
import argparse
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text as t

from database import get_session
import fine_subcategory as fs

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS dialog_fine_subcategory (
    dialog_id   INTEGER PRIMARY KEY REFERENCES dialogs(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    fine_bucket TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dfs_sub_bucket
    ON dialog_fine_subcategory (subcategory, fine_bucket);
"""

UPSERT = """
INSERT INTO dialog_fine_subcategory
    (dialog_id, category, subcategory, fine_bucket, computed_at)
VALUES
    (:dialog_id, :category, :subcategory, :fine_bucket, :computed_at)
ON CONFLICT (dialog_id) DO UPDATE SET
    category    = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    fine_bucket = EXCLUDED.fine_bucket,
    computed_at = EXCLUDED.computed_at
"""


def compute(date_from: str | None = None, date_to: str | None = None) -> int:
    s = get_session()
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            s.execute(t(stmt))
    s.commit()

    where = ""
    params: dict = {"subs": list(fs.SEGMENTED_SUBCATEGORIES)}
    if date_from:
        where += " AND d.dialog_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        where += " AND d.dialog_date <= :date_to"
        params["date_to"] = date_to

    # Последний analysis_result на диалог; берём только сегментируемые подкатегории.
    rows = s.execute(t(f"""
        WITH ar AS (
          SELECT DISTINCT ON (dialog_id)
                 dialog_id, category, subcategory, summary, rationale
          FROM analysis_results ORDER BY dialog_id, id DESC
        )
        SELECT d.id, ar.category, ar.subcategory, ar.summary, ar.rationale,
               d.messages_json
        FROM dialogs d
        JOIN ar ON ar.dialog_id = d.id
        WHERE ar.subcategory = ANY(:subs)
          {where}
    """), params).fetchall()

    now = datetime.now(timezone.utc)
    written = 0
    for did, category, subcategory, summary, rationale, mj in rows:
        try:
            msgs = json.loads(mj or "[]")
        except (ValueError, TypeError):
            msgs = []
        blob = fs.build_blob(msgs, summary, rationale)
        bucket = fs.classify(subcategory, blob)
        if bucket is None:
            continue
        s.execute(t(UPSERT), {
            "dialog_id": did,
            "category": category,
            "subcategory": fs.DISPLAY_SUB.get(subcategory, subcategory),
            "fine_bucket": bucket,
            "computed_at": now,
        })
        written += 1
    s.commit()
    return written


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default=None)
    ap.add_argument("--date-to", default=None)
    args = ap.parse_args()
    n = compute(args.date_from, args.date_to)
    print(f"dialog_fine_subcategory: written {n} rows.")


if __name__ == "__main__":
    main()
