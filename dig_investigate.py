"""Одноразовое исследование для доработки fine-сегментации:
1) все НЕ РАСПОЗНАНО диалоги двух подкатегорий (текст для новых правил),
2) сэмплы двух KYC-бакетов (отличаются ли),
3) диалоги бакета Пополнение/депозит,
4) структура подкатегорий категории KYC.
"""
import json
from collections import Counter

from sqlalchemy import text as t
from database import get_session
import fine_subcategory as fs

DF, DT = "2026-07-01", "2026-07-31"


def cohort_rows(sub):
    s = get_session()
    sql = """
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, subcategory, summary, rationale
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT tk.dialog_id, tk.side, tk.count, coalesce(tk.company,''),
           d.messages_json, ar.summary, ar.rationale, d.source
    FROM tickets tk
    JOIN ar ON ar.dialog_id=tk.dialog_id
    JOIN dialogs d ON d.id=tk.dialog_id
    WHERE tk.dialog_date>=:df AND tk.dialog_date<=:dt AND tk.methodology='E'
      AND ar.subcategory=:sub
    """
    return s.execute(t(sql), {"df": DF, "dt": DT, "sub": sub}).fetchall()


def blob_of(mj, summary, rationale):
    try:
        msgs = json.loads(mj or "[]")
    except Exception:
        msgs = []
    return fs.build_blob(msgs, summary, rationale)


print("=" * 90)
print("1) НЕ РАСПОЗНАНО")
for sub in (fs.REFUSAL_SUB, fs.HANGUP_SUB):
    print(f"\n### {sub}")
    for did, side, cnt, comp, mj, summ, rat, src in cohort_rows(sub):
        blob = blob_of(mj, summ, rat)
        if fs.classify(sub, blob) == fs.UNKNOWN:
            txt = " ".join(blob.split())[:600]
            print(f"\n-- dialog {did} [{side}, {src}, {comp[:20]}] --")
            print(f"   SUMMARY: {(summ or '')[:200]}")
            print(f"   BLOB: {txt}")

print("\n" + "=" * 90)
print("2) KYC-бакеты сравнение (по 6 сэмплов)")
for sub, bname in ((fs.REFUSAL_SUB, "Требуется верификация/KYC для выплаты"),
                   (fs.HANGUP_SUB, "Зависла из-за верификации/KYC")):
    print(f"\n### {sub} -> {bname}")
    n = 0
    for did, side, cnt, comp, mj, summ, rat, src in cohort_rows(sub):
        blob = blob_of(mj, summ, rat)
        if fs.classify(sub, blob) == bname:
            print(f"  d{did} [{side}]: {(summ or '')[:180]}")
            n += 1
            if n >= 6:
                break

print("\n" + "=" * 90)
print("3) Пополнение/депозит диалоги")
for did, side, cnt, comp, mj, summ, rat, src in cohort_rows(fs.HANGUP_SUB):
    blob = blob_of(mj, summ, rat)
    if fs.classify(fs.HANGUP_SUB, blob) == "Пополнение/депозит: зачисление на баланс платформы (заказчик)":
        print(f"  d{did} [{side}, {comp[:20]}]: {(summ or '')[:160]}")

print("\n" + "=" * 90)
print("4) KYC категория — подкатегории (analysis_results, июль E cohort)")
s = get_session()
rows = s.execute(t("""
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, category, subcategory
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT ar.subcategory, SUM(tk.count) tickets, COUNT(DISTINCT tk.dialog_id) dialogs
    FROM tickets tk JOIN ar ON ar.dialog_id=tk.dialog_id
    WHERE tk.dialog_date>=:df AND tk.dialog_date<=:dt AND tk.methodology='E'
      AND tk.category='KYC'
    GROUP BY ar.subcategory ORDER BY tickets DESC
"""), {"df": DF, "dt": DT}).fetchall()
for subcat, tickets, dialogs in rows:
    print(f"  {tickets:5d} т / {dialogs:4d} д  {subcat}")
