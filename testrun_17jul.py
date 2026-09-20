"""Test run for 17 July 2026:
  A) BASELINE cohort — per daily row (current production behaviour), shadows excluded.
  B) CROSS-DAY re-check of the awaiting_support tickets: stitch ALL daily rows of
     the client and test whether a substantive support answer exists inside the
     ticket window. Shows how many awaiting_support are cross-day false positives."""
import json
from collections import Counter
from datetime import date

from database import get_session, Dialog
import resolution_metrics as rm
import ticket_slices as ts
from shadow_dupes import shadow_dialog_ids

DAY = date(2026, 7, 17)
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def load(mj):
    return json.loads(mj) if isinstance(mj, str) else mj


def main():
    s = get_session()
    excl = shadow_dialog_ids(s)

    rows = s.query(Dialog).filter(Dialog.dialog_date == DAY).all()
    rows = [r for r in rows if r.id not in excl]
    n_shadow = sum(1 for r in s.query(Dialog).filter(Dialog.dialog_date == DAY).all() if r.id in excl)

    # ---------- A) BASELINE per-row cohort ----------
    status_counts = Counter()
    tickets = []  # (client_id, dialog_id, metrics, seg)
    for r in rows:
        msgs = load(r.messages_json)
        for m, seg in ts.slice_tickets(msgs):
            status_counts[m.status] += 1
            tickets.append((r.client_id, r.id, m, seg))

    total = sum(status_counts.values())
    print("=" * 70)
    print(f"17 ИЮЛЯ 2026 — BASELINE (по дневным строкам, тени исключены)")
    print(f"строк-диалогов: {len(rows)} (+{n_shadow} теней исключено)")
    print(f"тикетов (L1×L2): {total}")
    for st in ("resolved", "awaiting_client", "awaiting_support",
               "pending_department", "open"):
        c = status_counts.get(st, 0)
        pct = f"{100*c/total:.0f}%" if total else "-"
        print(f"  {st:20s}: {c:3d}  ({pct})")

    aw = [(cid, did, m, seg) for (cid, did, m, seg) in tickets
          if m.status == "awaiting_support"]
    print(f"\nawaiting_support тикетов: {len(aw)}")

    # ---------- B) CROSS-DAY re-check ----------
    print("\n" + "=" * 70)
    print("КРОСС-ДНЕВНАЯ ПРОВЕРКА awaiting_support (сшитый таймлайн клиента)")
    print("=" * 70)
    fixed = 0
    genuine = 0
    for cid, did, m, seg in aw:
        anchor = next((x for x in seg if x.is_client and x.substantive), None)
        if anchor is None:
            continue
        t0 = anchor.ts
        wend = ts.window_end(t0)

        allrows = s.query(Dialog).filter(Dialog.client_id == cid).all()
        allrows = [r for r in allrows if r.id not in excl]
        allmsgs = []
        for r in allrows:
            for mm in load(r.messages_json):
                allmsgs.append(mm)
        norm = rm.normalize(allmsgs)
        norm.sort(key=lambda x: x.ts)

        sup_in = [x for x in norm if (not x.is_client) and x.substantive
                  and t0 <= x.ts <= wend]
        # чей ход по последнему содержательному в окне
        in_win = [x for x in norm if t0 <= x.ts <= wend and x.substantive]
        last = in_win[-1] if in_win else None
        t0m = t0.astimezone(rm.MSK)
        verdict = "GENUINE awaiting_support"
        if sup_in:
            if last is not None and not last.is_client:
                verdict = "→ FIX: answered_in_window (awaiting_client/resolved)"
            else:
                verdict = "→ FIX: answered_in_window (last msg client)"
            fixed += 1
        else:
            genuine += 1
        head = (anchor.text or "").replace(chr(10), " ")[:90]
        print(f"\n[{cid} d{did}] {t0m:%m-%d %H:%M %a}")
        print(f"  req: {head!r}")
        print(f"  sup_in_window={len(sup_in)}  {verdict}")
        if sup_in:
            f = sup_in[0].ts.astimezone(rm.MSK)
            print(f"    first sup-in-win @ {f:%m-%d %H:%M}: "
                  f"{(sup_in[0].text or '').replace(chr(10),' ')[:110]!r}")

    print("\n" + "=" * 70)
    print(f"ИТОГ awaiting_support: {len(aw)} → ложных (ответ в окне): {fixed}, "
          f"реальных: {genuine}")
    if total:
        new_res_est = status_counts.get("resolved", 0) + fixed
        print(f"resolved (baseline): {status_counts.get('resolved',0)} "
              f"({100*status_counts.get('resolved',0)/total:.0f}%)  →  "
              f"после кросс-день (оценка +{fixed}): ~{new_res_est} "
              f"({100*new_res_est/total:.0f}%)")


if __name__ == "__main__":
    main()
