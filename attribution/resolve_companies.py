"""
Резолв executor_email -> company через Superset.

Источники маппинга (в порядке приоритета):
  1. `CONTRACTOR MAIN` (физически `mv.t_contractor_extended`, db_id=11).
     Поле company — `primary_active_company_name`, поле email — `email`.
     Это основной источник: исполнитель -> его активное юрлицо.
  2. `COMPANY MAIN` (физически `mv.t_company_metadata_extended`, db_id=11) —
     fallback для email'ов, которых нет в CONTRACTOR MAIN (обычно это
     корпоративные email представителей клиента, а не подрядчика).
     Поле company — `customer_first_company_name`, поле email — `customer_email`.

Алгоритм:
  1. Достаём DISTINCT executor_email из dialogs (где company IS NULL по умолчанию).
  2. Батчами по 300 спрашиваем Superset CONTRACTOR MAIN: email -> [(company_name, ...)].
  3. Фильтруем внутренние компании (Apzone / Rosburn / Efficient).
  4. Из оставшихся берём самую частую.
  5. Для email'ов без результата — добиваем через COMPANY MAIN.
  6. UPDATE dialogs.company.

Запуск (внутри webhook-контейнера):
  python -m resolve_dialog_companies [--only-empty] [--source flomni|chatapp|all]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from typing import Iterable

from sqlalchemy import text

from core.config import settings
from core.database import engine
from payouts_agent.superset_client import build_client_from_env


log = logging.getLogger(__name__)


# Внутренние юрлица MT — отбрасываем как «не атрибуция к клиенту».
INTERNAL_COMPANY_SUBSTRINGS = ("apzone", "rosburn", "efficient")

# В сколько email за один SQL-запрос в Superset ходим.
BATCH_SUPERSET = 300

# Superset coordinates.
SUPERSET_DATABASE_ID = 11
CONTRACTOR_TABLE = "mv.t_contractor_extended"
COMPANY_TABLE = "mv.t_company_metadata_extended"


def is_internal_company(name: str) -> bool:
    n = name.lower()
    return any(s in n for s in INTERNAL_COMPANY_SUBSTRINGS)


def pick_company(rows: Iterable[dict]) -> str | None:
    """Принимаем все строки contractor_main по одному email — выбираем company."""
    names = [(r.get("primary_active_company_name") or "").strip() for r in rows]
    external = [n for n in names if n and not is_internal_company(n)]
    if not external:
        return None
    return Counter(external).most_common(1)[0][0]


def fetch_companies(client, emails: list[str]) -> dict[str, list[dict]]:
    """Email -> список строк из contractor_main."""
    out: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(emails), BATCH_SUPERSET):
        batch = emails[i:i + BATCH_SUPERSET]
        in_list = ",".join("'" + e.replace("'", "''") + "'" for e in batch)
        sql = (
            f"SELECT email, primary_active_company_name "
            f"FROM {CONTRACTOR_TABLE} "
            f"WHERE email IN ({in_list})"
        )
        result = client.execute_sql(
            sql=sql,
            database_id=SUPERSET_DATABASE_ID,
            query_limit=10000,
            max_wait_seconds=120,
        )
        for row in result.get("rows", []):
            out[row["email"]].append(row)
        log.info("superset batch %d-%d done", i, i + len(batch) - 1)
    return out


def fetch_companies_fallback(client, emails: list[str]) -> dict[str, list[dict]]:
    """Fallback: email -> список строк из company_main (customer_email match)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(emails), BATCH_SUPERSET):
        batch = emails[i:i + BATCH_SUPERSET]
        in_list = ",".join("'" + e.replace("'", "''") + "'" for e in batch)
        sql = (
            f"SELECT customer_email, customer_first_company_name "
            f"FROM {COMPANY_TABLE} "
            f"WHERE customer_email IN ({in_list})"
        )
        result = client.execute_sql(
            sql=sql,
            database_id=SUPERSET_DATABASE_ID,
            query_limit=10000,
            max_wait_seconds=120,
        )
        for row in result.get("rows", []):
            # Нормализуем shape под pick_company(): кладём в ключ primary_active_company_name.
            out[row["customer_email"]].append({
                "primary_active_company_name": row.get("customer_first_company_name"),
            })
        log.info("superset COMPANY MAIN batch %d-%d done", i, i + len(batch) - 1)
    return out


def run(only_empty: bool, source_filter: str) -> None:
    where = ["executor_email IS NOT NULL"]
    params: dict[str, object] = {}
    if only_empty:
        where.append("company IS NULL")
    if source_filter != "all":
        where.append("source = :source")
        params["source"] = source_filter
    where_sql = "WHERE " + " AND ".join(where)

    with engine.connect() as conn:
        emails = [
            r[0] for r in conn.execute(
                text(f"SELECT DISTINCT executor_email FROM dialogs {where_sql}"),
                params,
            ).all()
        ]

    log.info("distinct executor_emails to resolve: %d", len(emails))
    if not emails:
        return

    client = build_client_from_env()
    client.authenticate()

    rows_per_email = fetch_companies(client, emails)
    log.info("contractor_main hits: %d / %d distinct emails",
             len(rows_per_email), len(emails))

    resolved: dict[str, str] = {}
    for email, rows in rows_per_email.items():
        picked = pick_company(rows)
        if picked is not None:
            resolved[email] = picked

    log.info("resolved from CONTRACTOR MAIN: %d emails", len(resolved))

    # Fallback: добиваем неразрезолвленные email через COMPANY MAIN.
    unresolved = [e for e in emails if e not in resolved]
    if unresolved:
        log.info("falling back to COMPANY MAIN for %d unresolved emails", len(unresolved))
        fallback_rows = fetch_companies_fallback(client, unresolved)
        fallback_added = 0
        for email, rows in fallback_rows.items():
            picked = pick_company(rows)
            if picked is not None:
                resolved[email] = picked
                fallback_added += 1
        log.info("resolved from COMPANY MAIN fallback: %d emails", fallback_added)

    log.info("resolved total %d emails -> company", len(resolved))

    # UPDATE dialogs батчами.
    BATCH_DB = 200
    items = list(resolved.items())
    updated_rows = 0
    for i in range(0, len(items), BATCH_DB):
        chunk = items[i:i + BATCH_DB]
        with engine.begin() as conn:
            for email, company in chunk:
                update_where = ["executor_email = :e"]
                if only_empty:
                    update_where.append("company IS NULL")
                if source_filter != "all":
                    update_where.append("source = :source")
                res = conn.execute(
                    text(
                        "UPDATE dialogs SET company = :c "
                        f"WHERE {' AND '.join(update_where)}"
                    ),
                    {"e": email, "c": company, **({"source": source_filter} if source_filter != "all" else {})},
                )
                updated_rows += res.rowcount or 0
        log.info("db batch %d-%d done (rows updated so far: %d)",
                 i, i + len(chunk) - 1, updated_rows)

    log.info("=== Done: emails_resolved=%d dialog_rows_updated=%d ===",
             len(resolved), updated_rows)

    top = Counter(resolved.values()).most_common(15)
    if top:
        log.info("Top-15 companies:")
        for name, n in top:
            log.info("  %-50s %d", name, n)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only-empty", action="store_true", default=True,
                        help="Только строки с company IS NULL (default: True)")
    parser.add_argument("--all-rows", dest="only_empty", action="store_false",
                        help="Перезаписать company даже если уже заполнено")
    parser.add_argument("--source", default="all",
                        choices=["all", "flomni", "telegram", "gmail", "chatapp"],
                        help="Фильтр по dialogs.source (default: all)")
    args = parser.parse_args()
    run(only_empty=args.only_empty, source_filter=args.source)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    main()
