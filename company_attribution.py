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

import extract_company_from_group_name
import extract_dialog_emails
import resolve_dialog_companies
from config import settings


log = logging.getLogger(__name__)


def run() -> None:
    log.info("=== Company attribution pipeline START ===")

    log.info("--- step 1/3: extract_dialog_emails ---")
    try:
        extract_dialog_emails.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("extract_dialog_emails failed")

    log.info("--- step 2/3: resolve_dialog_companies ---")
    try:
        resolve_dialog_companies.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("resolve_dialog_companies failed")

    log.info("--- step 3/3: extract_company_from_group_name ---")
    try:
        extract_company_from_group_name.run(dry_run=False)
    except Exception:
        log.exception("extract_company_from_group_name failed")

    log.info("=== Company attribution pipeline DONE ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
