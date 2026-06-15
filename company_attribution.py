"""
Company attribution daily job — five-step pipeline.

Ветки атрибуции (порядок важен — от дешёвых к дорогим):
  0. resolve_from_history.run        — самый дешёвый шаг: для повторно
                                       обращающихся client_id переносит
                                       email/company из исторических диалогов
                                       (dialogs + analytics_archive_jan_may).
                                       Снимает 50–70% повторных кейсов до
                                       того, как мы лезем в Superset.
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
  4. apply_company_aliases.run       — финальная унификация: применяет таблицу
                                       синонимов company_aliases, приводит дубли
                                       («Эктивейт» vs «ООО ЭКТИВЕЙТ») к одному
                                       каноническому имени.

Все шаги идемпотентны: шаги (0), (1) и (2) ходят только по пустым ячейкам по
умолчанию, (3) перетирает Superset-значения только если имя группы их явно
противоречит, (4) перетирает только те значения, что есть в company_aliases.

Запуск:
  python -m company_attribution
"""

from __future__ import annotations

import logging
import sys

import apply_company_aliases
import extract_company_from_group_name
import extract_dialog_emails
import resolve_dialog_companies
import resolve_from_history
from config import settings


log = logging.getLogger(__name__)


def run() -> None:
    log.info("=== Company attribution pipeline START ===")

    log.info("--- step 0/4: resolve_from_history ---")
    try:
        resolve_from_history.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("resolve_from_history failed")

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

    log.info("--- step 4/4: apply_company_aliases ---")
    try:
        apply_company_aliases.run(include_archive=False, dry_run=False)
    except Exception:
        log.exception("apply_company_aliases failed")

    log.info("=== Company attribution pipeline DONE ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
