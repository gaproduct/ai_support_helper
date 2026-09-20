"""Final July breakdown: every category -> subcategory with ticket & dialog
counts and customer/executor split. Uses the latest analysis_result per dialog
for the subcategory, tk.count as the ticket multiplier, one tickets row = one
dialog (methodology D)."""
from collections import defaultdict

from database import get_session
from sqlalchemy import text as t

LO, HI = "2026-07-01", "2026-07-31"


def main():
    s = get_session()
    sql = f"""
    WITH ar AS (
      SELECT DISTINCT ON (dialog_id) dialog_id, subcategory
      FROM analysis_results ORDER BY dialog_id, id DESC
    )
    SELECT tk.category, COALESCE(NULLIF(ar.subcategory,''),'—') AS sub,
           tk.side, tk.count
    FROM tickets tk
    JOIN ar ON ar.dialog_id = tk.dialog_id
    WHERE tk.dialog_date >= '{LO}' AND tk.dialog_date <= '{HI}'
      AND tk.methodology = 'E'
    """
    rows = s.execute(t(sql)).fetchall()

    cat_t = defaultdict(int)
    cat_d = defaultdict(int)
    sub_t = defaultdict(int)          # (cat,sub) -> tickets
    sub_d = defaultdict(int)          # (cat,sub) -> dialogs
    sub_side = defaultdict(lambda: defaultdict(int))
    cat_side = defaultdict(lambda: defaultdict(int))

    for cat, sub, side, cnt in rows:
        cat_t[cat] += cnt
        cat_d[cat] += 1
        cat_side[cat][side] += cnt
        sub_t[(cat, sub)] += cnt
        sub_d[(cat, sub)] += 1
        sub_side[(cat, sub)][side] += cnt

    tot_t = sum(cat_t.values())
    tot_d = sum(cat_d.values())
    print(f"ИЮЛЬ 2026 — тикетов: {tot_t}; диалогов: {tot_d}\n")

    hdr = f"{'тикет':>6} {'диал':>5} {'cust':>5} {'exec':>5}  {'%т':>4}  категория / подкатегория"
    print(hdr)
    print("-" * len(hdr))

    for cat in sorted(cat_t, key=lambda c: -cat_t[c]):
        pct = 100 * cat_t[cat] / tot_t if tot_t else 0
        print(f"{cat_t[cat]:6d} {cat_d[cat]:5d} "
              f"{cat_side[cat]['customer']:5d} {cat_side[cat]['executor']:5d} "
              f"{pct:4.0f}  {cat}")
        subs = [k for k in sub_t if k[0] == cat]
        if len(subs) == 1 and subs[0][1] == "—":
            continue
        for (c, sub) in sorted(subs, key=lambda k: -sub_t[k]):
            print(f"{sub_t[(c,sub)]:6d} {sub_d[(c,sub)]:5d} "
                  f"{sub_side[(c,sub)]['customer']:5d} {sub_side[(c,sub)]['executor']:5d} "
                  f"      └─ {sub}")
        print()


if __name__ == "__main__":
    main()
