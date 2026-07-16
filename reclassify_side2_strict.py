"""
Side-only re-classification pass — STRICT variant.

Flips customer -> executor only when BOTH classifiers agree it is executor:
  1. few-shot inbound-only classifier (reclassify_side2._classify_side)
  2. the main full-dialog classifier (ai_analysis SYSTEM_PROMPT)
Noise dialogs (only /start, greetings, thanks, single tokens) are skipped —
they carry no signal and must not get an arbitrary side.

On flip, category is remapped via ai_analysis.CROSS_SIDE_MAP. Only the latest
AnalysisResult per dialog is updated.
"""

import logging
import re
import sys

from sqlalchemy import text as sqlt

import ai_analysis as ai
import reclassify_side2 as r2
from database import AnalysisResult, get_session

log = logging.getLogger("reclassify_side2_strict")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Tokens that carry no side signal — stripped before the noise-length check.
_NOISE_TOKENS = re.compile(
    r"/start|спасибо|благодар\w*|пожалуйста|добр\w+ (день|утр\w|вечер)|здравствуйте|"
    r"привет|hi|hello|thanks|thank you|ok|окей|ок|да|нет|ага|угу|проверьте|"
    r"\bжаль\b|\+|[\d\s\W_]+",
    re.IGNORECASE,
)


def _is_noise(inbound: str) -> bool:
    """True if inbound has no meaningful content beyond greetings/commands/digits."""
    stripped = _NOISE_TOKENS.sub("", inbound or "")
    return len(stripped.strip()) < 15


# Group-chat / relay markers — these are customer-side coordination threads,
# never a single executor writing about themselves.
_AUTHOR_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_]+)", re.MULTILINE)
_RELAY_RE = re.compile(
    r"исполнител\w*\s*[:#]|получател|контрагент|задач[аиу]\s*[:#№]?\s*\d{3,}|"
    r"пользовател\w*\s+\S+@",
    re.IGNORECASE,
)

# Customer-only actions: only a payer tops up balance / imports tasks.
_CUSTOMER_ACTION_RE = re.compile(
    r"пополнен\w*.{0,30}(счёт|счет|баланс|usdt|кошел|депозит)|"
    r"(счёт|счет|баланс|usdt|кошел|депозит).{0,30}пополнен\w*|"
    r"импорт\w*\s+задач|услуга не найдена|создани\w*\s+задач",
    re.IGNORECASE | re.DOTALL,
)


def _is_group_or_relay(inbound: str) -> bool:
    """True for customer coordination threads: multiple @authors, relay of a
    third-party executor/task ('Исполнитель: x@', 'Задача 754454', 'контрагент'),
    or customer-only platform actions (balance top-up, task import)."""
    if len(set(_AUTHOR_RE.findall(inbound or ""))) >= 2:
        return True
    if _RELAY_RE.search(inbound or ""):
        return True
    return bool(_CUSTOMER_ACTION_RE.search(inbound or ""))


def run(date_from: str, date_to: str) -> None:
    with get_session() as db:
        rows = db.execute(sqlt("""
            SELECT ar.id AS ar_id, d.id AS dialog_id, ar.category AS old_cat,
                   d.messages_text
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) l
                 ON l.dialog_id = ar.dialog_id AND l.mx = ar.id
            WHERE d.dialog_date BETWEEN :a AND :b
              AND ar.side = 'customer'
              AND ar.category <> 'Тест'
            ORDER BY d.dialog_date, d.id
        """), {"a": date_from, "b": date_to}).fetchall()

    log.info("Loaded %d customer dialogs to re-check (strict).", len(rows))
    flips = skipped_noise = 0
    for i, r in enumerate(rows, 1):
        inbound = r2._inbound_only(r.messages_text or "")
        if not inbound.strip():
            continue
        if _is_noise(inbound):
            skipped_noise += 1
            continue
        if _is_group_or_relay(inbound):
            continue  # customer coordination / relay thread → keep customer
        if r2._classify_side(inbound) != "executor":
            continue
        new_cat = ai._validate_category("executor", r.old_cat)
        with get_session() as db:
            obj = db.get(AnalysisResult, r.ar_id)
            obj.side = "executor"
            obj.category = new_cat
            db.commit()
        flips += 1
        log.info("[%d/%d] dialog %d: customer->executor, cat %s->%s",
                 i, len(rows), r.dialog_id, r.old_cat, new_cat)
        if i % 50 == 0:
            log.info("progress %d/%d flips=%d noise_skipped=%d", i, len(rows), flips, skipped_noise)

    log.info("Done. flips=%d noise_skipped=%d / %d.", flips, skipped_noise, len(rows))


if __name__ == "__main__":
    df = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
    dt = sys.argv[2] if len(sys.argv) > 2 else "2026-06-25"
    run(df, dt)
