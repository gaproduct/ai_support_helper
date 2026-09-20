"""Дамп содержимого диалогов категории KYC по подкатегориям (июль, E cohort),
чтобы спроектировать fine-бакеты для KYC."""
import json
from collections import Counter

from sqlalchemy import text as t
from database import get_session

DF, DT = "2026-07-01", "2026-07-31"
s = get_session()
rows = s.execute(t("""
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, category, subcategory, summary, rationale
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT tk.dialog_id, tk.side, ar.subcategory, ar.summary, ar.rationale, d.messages_json
    FROM tickets tk
    JOIN ar ON ar.dialog_id=tk.dialog_id
    JOIN dialogs d ON d.id=tk.dialog_id
    WHERE tk.dialog_date>=:df AND tk.dialog_date<=:dt AND tk.methodology='E'
      AND tk.category='KYC'
    ORDER BY ar.subcategory, tk.dialog_id
"""), {"df": DF, "dt": DT}).fetchall()

cur = None
for did, side, sub, summ, rat, mj in rows:
    if sub != cur:
        print("\n" + "=" * 90 + f"\n### {sub}\n" + "=" * 90)
        cur = sub
    try:
        msgs = json.loads(mj or "[]")
    except Exception:
        msgs = []
    blob = " ".join(m.get("text", "") for m in msgs)
    blob = " ".join(blob.split())[:400]
    print(f"\n-- d{did} [{side}] --")
    print(f"  S: {(summ or '')[:180]}")
    print(f"  B: {blob}")
