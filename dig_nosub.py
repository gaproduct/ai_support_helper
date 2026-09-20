"""Дамп диалогов без AI-подкатегории в категориях Выплаты / Запрос документов /
Вопросы по работе в сервисе, чтобы назначить им подкатегории по одиночке."""
import json, sys
from sqlalchemy import text as t
from database import get_session

CATS = ["Выплаты и проблемы с ними", "Запрос документов", "Вопросы по работе в сервисе"]
s = get_session()

for cat in CATS:
    print("\n" + "#" * 95 + f"\n### {cat}\n" + "#" * 95)
    print("-- существующие подкатегории --")
    for r in s.execute(t("""
      WITH ar AS (SELECT DISTINCT ON (dialog_id) dialog_id, subcategory FROM analysis_results ORDER BY dialog_id, id DESC)
      SELECT COALESCE(NULLIF(ar.subcategory,''),'(без подкатегории)') sub, SUM(tk.count) n, COUNT(DISTINCT tk.dialog_id) d
      FROM tickets tk JOIN ar ON ar.dialog_id=tk.dialog_id
      WHERE tk.methodology='E' AND tk.dialog_date BETWEEN '2026-07-01' AND '2026-07-31' AND tk.category=:c
      GROUP BY 1 ORDER BY n DESC
    """), {"c": cat}).fetchall():
        print(f"   {r[1]:>4}/{r[2]:>3}д  {r[0]}")
    rows = s.execute(t("""
      WITH ar AS (SELECT DISTINCT ON (dialog_id) dialog_id, subcategory, summary FROM analysis_results ORDER BY dialog_id, id DESC)
      SELECT tk.dialog_id, tk.side, ar.summary, d.messages_json
      FROM tickets tk JOIN ar ON ar.dialog_id=tk.dialog_id JOIN dialogs d ON d.id=tk.dialog_id
      WHERE tk.methodology='E' AND tk.dialog_date BETWEEN '2026-07-01' AND '2026-07-31' AND tk.category=:c
        AND (ar.subcategory IS NULL OR ar.subcategory='')
      ORDER BY tk.dialog_id
    """), {"c": cat}).fetchall()
    print(f"-- без подкатегории: {len(rows)} --")
    for did, side, summ, mj in rows:
        try: msgs = json.loads(mj or "[]")
        except: msgs = []
        blob = " ".join(m.get("text", "") for m in msgs); blob = " ".join(blob.split())[:320]
        print(f"\n-- d{did} [{side}] --")
        print(f"  S: {(summ or '')[:190]}")
        print(f"  B: {blob}")
