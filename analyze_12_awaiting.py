"""One-off: for each of the 12 awaiting_support clients (16 July post-dedup
cohort), anchor on the FIRST client-substantive message dated 16 July, compute
the ticket window, and check for a support answer inside that window across ALL
daily rows (cross-day stitched, shadows excluded)."""
import json
from datetime import date

from database import get_session, Dialog
import resolution_metrics as rm
import ticket_slices as ts
from shadow_dupes import shadow_dialog_ids

CLIENTS = [
    "fc1e4ebf", "cb653b6f", "96146987", "62b1ed0a", "25ee7201",
    "-4943891199", "0c4575cc", "e21a3b7a", "-1003922942081",
    "38df8b52", "e46cf78a", "-1003404846326",
]
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ANCHOR = date(2026, 7, 16)


def load(mj):
    return json.loads(mj) if isinstance(mj, str) else mj


def main():
    s = get_session()
    excl = shadow_dialog_ids(s)

    for cid in CLIENTS:
        rows = s.query(Dialog).filter(Dialog.client_id.like(f"%{cid}%")).all()
        rows = [r for r in rows if r.id not in excl]
        if not rows:
            print(f"\n===== {cid}: NO ROWS (all shadow/missing) =====")
            continue

        allmsgs = []
        for r in rows:
            for m in load(r.messages_json):
                allmsgs.append(m)
        norm = rm.normalize(allmsgs)
        norm.sort(key=lambda x: x.ts)

        # anchor = first client substantive whose MSK date == 16 July
        anchor = next(
            (m for m in norm if m.is_client and m.substantive
             and m.ts.astimezone(rm.MSK).date() == ANCHOR),
            None,
        )
        print(f"\n{'='*78}")
        print(f"CLIENT {cid} | rows={len(rows)} ids={[r.id for r in rows]}")
        if anchor is None:
            print("  !! no client-substantive msg on 16 July among non-shadow rows")
            continue

        t0 = anchor.ts
        wend = ts.window_end(t0)
        t0m, wm = t0.astimezone(rm.MSK), wend.astimezone(rm.MSK)
        print(f"ANCHOR (16 Jul req): {t0m:%Y-%m-%d %H:%M} МСК ({WD[t0m.weekday()]})")
        print(f"WINDOW END: {wm:%Y-%m-%d %H:%M} МСК ({WD[wm.weekday()]})")

        win = [m for m in norm if t0 <= m.ts <= wend]
        sup_in = [m for m in win if (not m.is_client) and m.substantive]
        sup_after = [m for m in norm if (not m.is_client) and m.substantive and m.ts > wend]
        print(f"SUPPORT substantive IN window: {len(sup_in)} | AFTER window: {len(sup_after)}")
        if sup_in:
            f = sup_in[0].ts.astimezone(rm.MSK)
            print(f"  IN  @ {f:%m-%d %H:%M}: {(sup_in[0].text or '').replace(chr(10),' ')[:160]!r}")
        if sup_after:
            f = sup_after[0].ts.astimezone(rm.MSK)
            print(f"  AFT @ {f:%m-%d %H:%M}: {(sup_after[0].text or '').replace(chr(10),' ')[:160]!r}")

        # transcript from anchor to end of window + a little tail
        print("--- WINDOW TRANSCRIPT (anchor .. window_end) ---")
        for m in win:
            role = "КЛИЕНТ" if m.is_client else "САППОРТ"
            t = m.ts.astimezone(rm.MSK)
            sub = "" if m.substantive else " (служ)"
            print(f"[{t:%m-%d %H:%M} {WD[t.weekday()]}] {role}{sub}: "
                  f"{(m.text or '').replace(chr(10),' ')[:220]}")


if __name__ == "__main__":
    main()
