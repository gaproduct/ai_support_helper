"""Compare July ticket counts D vs E per category (+ the two payment
subcategories that drove the inflation). Dialog count is identical; only the
customer-side ticket multiplier changes."""
from collections import defaultdict

from database import get_session
from sqlalchemy import text as t

LO, HI = "2026-07-01", "2026-07-31"


def load(meth):
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
      AND tk.methodology = '{meth}'
    """
    cat_t = defaultdict(int); cat_d = defaultdict(int)
    sub_t = defaultdict(int)
    for cat, sub, side, cnt in s.execute(t(sql)).fetchall():
        cat_t[cat] += cnt; cat_d[cat] += 1
        sub_t[(cat, sub)] += cnt
    return cat_t, cat_d, sub_t


def main():
    dt, dd, dsub = load("D")
    et, ed, esub = load("E")

    print("=== ПО КАТЕГОРИЯМ: тикеты D → E (диалоги неизменны) ===\n")
    print(f"{'диал.':>6} {'D тик':>7} {'E тик':>7} {'Δ':>6}  категория")
    print("-" * 60)
    for cat in sorted(dt, key=lambda c: -et[c]):
        d = dd[cat]; D = dt[cat]; E = et[cat]
        print(f"{d:6d} {D:7d} {E:7d} {E-D:6d}  {cat}")
    print("-" * 60)
    print(f"{sum(dd.values()):6d} {sum(dt.values()):7d} {sum(et.values()):7d} "
          f"{sum(et.values())-sum(dt.values()):6d}  ИТОГО")

    print("\n=== Подкатегории выплат (главный источник разницы) ===\n")
    print(f"{'D тик':>7} {'E тик':>7} {'Δ':>6}  подкатегория")
    print("-" * 60)
    keys = sorted(set(dsub) | set(esub),
                  key=lambda k: -(esub.get(k, 0)))
    for k in keys:
        if k[0] != "Выплаты и проблемы с ними":
            continue
        D = dsub.get(k, 0); E = esub.get(k, 0)
        if D == 0 and E == 0:
            continue
        print(f"{D:7d} {E:7d} {E-D:6d}  {k[1]}")


if __name__ == "__main__":
    main()
