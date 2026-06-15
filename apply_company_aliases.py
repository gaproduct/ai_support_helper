"""
Apply company aliases — финальный шаг атрибуции (step 4/4).

Источник: таблица company_aliases (alias -> canonical).

Логика:
  1. JOIN dialogs ↔ company_aliases по dialogs.company = alias.
  2. Перезаписываем dialogs.company на canonical для совпадений.
  3. Аналогично для analytics_archive_jan_may (опционально, по флагу).

Идемпотентно: повторный запуск не делает ничего, потому что после первой
прогона dialogs.company уже равен canonical, и в company_aliases нет строки
`alias = canonical` (мы такие пары не вставляем).

Запуск:
  python -m apply_company_aliases [--archive] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text

from config import settings
from database import engine


log = logging.getLogger(__name__)


def run(include_archive: bool = False, dry_run: bool = False) -> None:
    log.info("Applying company aliases (include_archive=%s, dry_run=%s).",
             include_archive, dry_run)

    with engine.connect() as conn:
        n_aliases = conn.execute(
            text("SELECT COUNT(*) FROM company_aliases")
        ).scalar() or 0
    log.info("company_aliases entries: %d", n_aliases)
    if n_aliases == 0:
        log.info("Nothing to apply — table is empty.")
        return

    # ── dialogs ──────────────────────────────────────────────────────────────
    with engine.connect() as conn:
        preview = conn.execute(text("""
            SELECT COUNT(*) FROM dialogs d
            JOIN company_aliases a ON a.alias = d.company
            WHERE d.company <> a.canonical
        """)).scalar() or 0
    log.info("dialogs rows to rewrite: %d", preview)

    if not dry_run and preview > 0:
        with engine.begin() as conn:
            res = conn.execute(text("""
                UPDATE dialogs d
                   SET company = a.canonical
                  FROM company_aliases a
                 WHERE d.company = a.alias
                   AND d.company <> a.canonical
            """))
            log.info("dialogs updated: %d", res.rowcount)

    # ── analytics_archive_jan_may (по флагу) ─────────────────────────────────
    if include_archive:
        with engine.connect() as conn:
            arch_preview = conn.execute(text("""
                SELECT COUNT(*) FROM analytics_archive_jan_may a
                JOIN company_aliases al ON al.alias = a.company
                WHERE a.company <> al.canonical
            """)).scalar() or 0
        log.info("archive rows to rewrite: %d", arch_preview)

        if not dry_run and arch_preview > 0:
            with engine.begin() as conn:
                res = conn.execute(text("""
                    UPDATE analytics_archive_jan_may a
                       SET company = al.canonical
                      FROM company_aliases al
                     WHERE a.company = al.alias
                       AND a.company <> al.canonical
                """))
                log.info("archive updated: %d", res.rowcount)

    log.info("=== Done ===")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="store_true",
                        help="Также перезаписать analytics_archive_jan_may.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать сколько строк будет обновлено, без UPDATE.")
    args = parser.parse_args()
    run(include_archive=args.archive, dry_run=args.dry_run)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    main()
