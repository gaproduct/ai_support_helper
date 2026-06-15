"""
Resolve dialog email/company через исторические записи по тому же client_id.

Нулевой шаг атрибуции компании. Для диалога с пустым executor_email и/или
company берёт самые частые executor_email и company по тому же (source,
client_id) из:
  - живой таблицы `dialogs` (другие периоды того же исполнителя)
  - архива `analytics_archive_jan_may`

Полезен потому что:
  - Исполнители повторно пишут в поддержку. У client_id одного и того же
    Telegram-аккаунта (ChatApp) или Flomni-UUID почти всегда стабильный email.
  - Не требует ни OpenAI, ни Superset — просто SQL по своей БД.
  - Срабатывает раньше всех остальных шагов и убирает 50–70% повторных
    диалогов из работы дорогих шагов 1 (email regex) и 2 (Superset).

Алгоритм:
  1. Берём DISTINCT (source, client_id) среди диалогов с пустым email и/или
     company.
  2. Одним SQL агрегируем по этим парам Counter(executor_email) и
     Counter(company) из dialogs+archive.
  3. UPDATE dialogs: проставляем executor_email там где пусто, и company
     там где пусто.

Запуск:
  python -m resolve_from_history [--only-empty] [--source flomni|chatapp|all]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from config import settings
from database import engine


log = logging.getLogger(__name__)


def run(only_empty: bool, source_filter: str) -> None:
    log.info("Starting historical client_id resolve (only_empty=%s, source=%s).",
             only_empty, source_filter)

    # ── 1. Кандидаты ────────────────────────────────────────────────────────
    where = []
    params: dict[str, object] = {}
    if only_empty:
        where.append("(company IS NULL OR company = '' "
                     "OR executor_email IS NULL OR executor_email = '')")
    if source_filter != "all":
        where.append("source = :source")
        params["source"] = source_filter
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, source, client_id, executor_email, company
            FROM dialogs
            {where_sql}
        """), params).all()

    log.info("Candidate dialogs: %d", len(rows))
    if not rows:
        return

    # (source, client_id) -> [(dialog_id, has_email, has_company), ...]
    candidate_pairs: dict[tuple[str, str], list[tuple[int, bool, bool]]] = defaultdict(list)
    for r in rows:
        if not r.client_id:
            continue
        candidate_pairs[(r.source, str(r.client_id))].append((
            r.id,
            bool(r.executor_email and r.executor_email.strip()),
            bool(r.company and r.company.strip()),
        ))

    log.info("Distinct (source, client_id) pairs: %d", len(candidate_pairs))
    if not candidate_pairs:
        return

    # ── 2. Достаём историю одним проходом по dialogs + archive ──────────────
    distinct_cids_by_source: dict[str, list[str]] = defaultdict(list)
    for src, cid in candidate_pairs.keys():
        distinct_cids_by_source[src].append(cid)

    emails_by_pair: dict[tuple[str, str], Counter] = defaultdict(Counter)
    companies_by_pair: dict[tuple[str, str], Counter] = defaultdict(Counter)

    with engine.connect() as conn:
        for src, cids in distinct_cids_by_source.items():
            # dialogs (включая текущие пустые — они просто не дадут вклада)
            hist_d = conn.execute(text("""
                SELECT client_id, executor_email, company
                FROM dialogs
                WHERE source = :s
                  AND client_id = ANY(:cids)
                  AND ((executor_email IS NOT NULL AND executor_email <> '')
                    OR (company IS NOT NULL AND company <> ''))
            """), {"s": src, "cids": cids}).all()
            for h in hist_d:
                key = (src, str(h.client_id))
                if h.executor_email:
                    emails_by_pair[key][h.executor_email.strip().lower()] += 1
                if h.company:
                    companies_by_pair[key][h.company.strip()] += 1

            # архив (chat_id == client_id)
            hist_a = conn.execute(text("""
                SELECT chat_id AS client_id, executor_email, company
                FROM analytics_archive_jan_may
                WHERE source = :s
                  AND chat_id = ANY(:cids)
                  AND ((executor_email IS NOT NULL AND executor_email <> '')
                    OR (company IS NOT NULL AND company <> ''))
            """), {"s": src, "cids": cids}).all()
            for h in hist_a:
                key = (src, str(h.client_id))
                if h.executor_email:
                    emails_by_pair[key][h.executor_email.strip().lower()] += 1
                if h.company:
                    companies_by_pair[key][h.company.strip()] += 1

    pairs_with_history = sum(
        1 for k in candidate_pairs if emails_by_pair[k] or companies_by_pair[k]
    )
    log.info("Pairs with historical resolution: %d / %d",
             pairs_with_history, len(candidate_pairs))

    # ── 3. UPDATE: проставляем где пусто ────────────────────────────────────
    updated_rows = 0
    filled_email = 0
    filled_company = 0
    with engine.begin() as conn:
        for key, dialogs in candidate_pairs.items():
            email_counter = emails_by_pair.get(key)
            company_counter = companies_by_pair.get(key)
            top_email = email_counter.most_common(1)[0][0] if email_counter else None
            top_company = company_counter.most_common(1)[0][0] if company_counter else None
            if not top_email and not top_company:
                continue
            for did, has_email, has_company in dialogs:
                sets: list[str] = []
                params = {"did": did}
                if top_email and not has_email:
                    sets.append("executor_email = :email")
                    params["email"] = top_email
                if top_company and not has_company:
                    sets.append("company = :company")
                    params["company"] = top_company
                if not sets:
                    continue
                res = conn.execute(
                    text(f"UPDATE dialogs SET {', '.join(sets)} WHERE id = :did"),
                    params,
                )
                if res.rowcount:
                    updated_rows += 1
                    if "email" in params:
                        filled_email += 1
                    if "company" in params:
                        filled_company += 1

    log.info("=== Done: rows_updated=%d filled_email=%d filled_company=%d ===",
             updated_rows, filled_email, filled_company)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only-empty", action="store_true", default=True,
                        help="Только строки где email или company пусты (default: True)")
    parser.add_argument("--all-rows", dest="only_empty", action="store_false",
                        help="Идти по всем строкам, даже заполненным")
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
