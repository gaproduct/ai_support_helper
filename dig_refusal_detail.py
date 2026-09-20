"""Show WHY the 'Комплаенс/санкции/риски' bucket has few dialogs but many
tickets: list every dialog in it with its per-dialog tk.count, side, company."""
import json
import re
from collections import Counter

from database import get_session
from sqlalchemy import text as t

CAT = "Выплаты и проблемы с ними"
SUB = "Уточнение причин отказа по платежу"

# Same first-match-wins ordering as dig_refusal.py, but we only need to know
# which dialogs fall into the compliance bucket, so replicate its patterns.
COMPLIANCE = [re.compile(p, re.I) for p in [
    r"комплаенс", r"complianc", r"санкц", r"sanction", r"риск-", r"служба безопасн",
    r"ограничени.{0,20}комплаенс", r"проверк.{0,15}безопасн", r"финмониторинг",
    r"115-фз", r"фз.{0,5}115",
]]
# earlier buckets that would win before compliance (specificity)
BEFORE = [re.compile(p, re.I) for p in [
    r"не поддерживается картой", r"тип операции не поддерж", r"card.*not support",
    r"операция не поддерж", r"карта не поддерж",
    r"треть.{0,10}лиц", r"перевод.{0,15}(друг|чуж|ин)", r"не.{0,10}свою карт",
    r"карта должна.{0,15}принадлеж", r"только.{0,10}на свою", r"чужую карт",
    r"на имя.{0,15}(получател|исполнител)", r"фио.{0,15}не совпад",
    r"карт.{0,10}(друг|подруг)", r"my friend", r"friend.{0,10}card",
    r"превышен лимит", r"лимит.{0,20}(карт|реквизит|росси|выплат|операц)",
    r"лимит самозанят", r"лимит нпд", r"достигнут лимит", r"exceeded.*limit",
    r"суточн.{0,10}лимит", r"месячн.{0,10}лимит", r"годов.{0,10}лимит",
    r"реквизит.{0,15}заблокир", r"карт.{0,10}заблокир", r"счет.{0,10}заблокир",
    r"заблокирован.{0,15}(карт|реквизит|счет)",
    r"неверн.{0,15}реквизит", r"некорректн.{0,15}(реквизит|карт|счет|номер)",
    r"неправильн.{0,15}(реквизит|карт|номер)", r"ошибка в реквизит",
    r"реквизиты указаны неверно", r"неверный номер карты", r"invalid.*(card|account|iban)",
    r"проверьте.{0,15}реквизит",
]]


def hit(pats, text):
    return any(p.search(text) for p in pats)


def main():
    s = get_session()
    sql = f"""
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, subcategory, summary, rationale, side
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT tk.dialog_id, tk.side, tk.count, coalesce(tk.company,''),
           d.messages_json, ar.summary, ar.rationale, d.source
    FROM tickets tk
    JOIN ar ON ar.dialog_id=tk.dialog_id
    JOIN dialogs d ON d.id=tk.dialog_id
    WHERE tk.dialog_date>='2026-07-01' AND tk.dialog_date<='2026-07-31' AND tk.methodology='E'
      AND tk.category='{CAT}' AND ar.subcategory='{SUB}'
    """
    rows = s.execute(t(sql)).fetchall()

    hits = []
    for did, side, cnt, company, mj, summary, rationale, source in rows:
        try:
            msgs = json.loads(mj or "[]")
        except Exception:
            msgs = []
        blob = " ".join(m.get("text", "") for m in msgs)
        blob += " " + (summary or "") + " " + (rationale or "")
        if hit(BEFORE, blob):
            continue
        if hit(COMPLIANCE, blob):
            n_exec_emails = len({(m.get("executor_email") or "").lower()
                                 for m in msgs if m.get("executor_email")})
            hits.append((did, side, cnt, company, source, n_exec_emails, len(msgs)))

    hits.sort(key=lambda x: -x[2])
    print(f"Диалогов в бакете 'Комплаенс': {len(hits)}; тикетов: {sum(h[2] for h in hits)}\n")
    print(f"{'dialog':>7} {'side':>9} {'tickets':>7} {'exec_em':>7} {'msgs':>5}  source / company")
    print("-" * 80)
    for did, side, cnt, company, source, nem, nmsg in hits:
        print(f"{did:7d} {side:>9} {cnt:7d} {nem:7d} {nmsg:5d}  {source:8s} {company[:30]}")

    by_side = Counter()
    tk_side = Counter()
    for did, side, cnt, *_ in hits:
        by_side[side] += 1
        tk_side[side] += cnt
    print("\nПо стороне:")
    for sd in ("customer", "executor"):
        print(f"  {sd:9s}: диалогов {by_side[sd]:3d}, тикетов {tk_side[sd]:4d}")


if __name__ == "__main__":
    main()
