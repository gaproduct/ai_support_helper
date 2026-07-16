"""
Company attribution daily job — three-step pipeline.

Ветки атрибуции (порядок важен):
  1. extract_dialog_emails.run       — вытаскивает executor_email из messages_text
                                       / messages_json (regex + ChatApp fromUser.email).
  2. resolve_dialog_companies.run    — резолвит executor_email -> company через
                                       Superset CONTRACTOR MAIN (primary_active_company_name).
                                       Fallback: COMPANY MAIN (customer_first_company_name).
                                       Внутренние компании (Apzone/Rosburn/Efficient)
                                       отфильтрованы.
  3. extract_company_from_group_name.run — для chat-name'ов вида «КЛИЕНТ × MT»
                                       (chatapp.chat_name / flomni incoming_messages.name)
                                       вытаскивает первый внешний токен и ПЕРЕЗАПИСЫВАЕТ
                                       company (короткая «человеческая» форма приоритетна
                                       над Superset).

Все три шага идемпотентны: шаги (1) и (2) ходят только по NULL'ам по умолчанию,
(3) перетирает Superset-значения только если имя группы их явно противоречит.

Запуск:
  python -m company_attribution
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import text

import extract_company_from_group_name
import extract_dialog_emails
import resolve_dialog_companies
from company_aliases import canonical_company
from config import settings
from database import engine


log = logging.getLogger(__name__)


def normalize_companies() -> None:
    """Свести синонимы dialogs.company к каноничному имени (company_aliases).

    Идёт по distinct-значениям и UPDATE'ит только те, что реально меняются,
    поэтому идемпотентно и дёшево."""
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT company FROM dialogs WHERE company IS NOT NULL"
        ))]

    remap = {n: canonical_company(n) for n in names}
    remap = {old: new for old, new in remap.items() if new != old}

    if not remap:
        log.info("normalize_companies: nothing to remap.")
        return

    with engine.begin() as conn:
        for old, new in remap.items():
            res = conn.execute(
                text("UPDATE dialogs SET company = :new WHERE company = :old"),
                {"new": new, "old": old},
            )
            log.info("normalize_companies: %r -> %r (%d rows)", old, new, res.rowcount or 0)


def run() -> None:
    log.info("=== Company attribution pipeline START ===")

    log.info("--- step 1/4: extract_dialog_emails ---")
    try:
        extract_dialog_emails.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("extract_dialog_emails failed")

    log.info("--- step 2/4: resolve_dialog_companies ---")
    try:
        resolve_dialog_companies.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("resolve_dialog_companies failed")

    log.info("--- step 3/4: extract_company_from_group_name ---")
    try:
        extract_company_from_group_name.run(dry_run=False)
    except Exception:
        log.exception("extract_company_from_group_name failed")

    log.info("--- step 4/4: normalize_companies ---")
    try:
        normalize_companies()
    except Exception:
        log.exception("normalize_companies failed")

    log.info("=== Company attribution pipeline DONE ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
