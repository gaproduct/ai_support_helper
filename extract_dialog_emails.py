"""
Извлечение email исполнителя из текста диалогов.

Сейчас работает только по таблице `dialogs`. Для каждой строки:
  1. Собираем email-кандидатов из messages_text (regex) + из messages_json
     (поле fromUser.email для ChatApp).
  2. Приводим к lowercase, отсекаем blacklist-домены (наши агенты/системные).
  3. Берём САМЫЙ ЧАСТЫЙ из оставшихся (при ничьей — первый встретившийся).
  4. UPDATE dialogs.executor_email.

Запуск:
  python -m extract_dialog_emails [--only-empty] [--source flomni|chatapp|all]

  --only-empty   только строки с executor_email IS NULL (default: True)
  --source       фильтр по dialogs.source (default: all)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from typing import Iterable

from sqlalchemy import text

from config import settings
from database import engine


log = logging.getLogger(__name__)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# В messages_text newlines/tabs сохранены как ЛИТЕРАЛЫ:
#   '\n' (2 символа) — обычный escape
#   '\\n' (3 символа) — двойное JSON-экранирование
# Иначе regex ловит обрывок типа 'nanuta-kotenok@mail.ru' вместо 'anuta-kotenok@mail.ru'.
_LITERAL_WHITESPACE_RE = re.compile(r"\\{1,2}[nrt]")

# Домены нашей стороны — НЕ executor.
# Точные совпадения для доменов, не входящих в брендовые префиксы ниже.
BLACKLIST_DOMAINS_EXACT: frozenset[str] = frozenset({"chatapp.online"})

# Префиксы доменов наших брендов — перекрывают все TLD и субдомены:
#   @madetask.com, @madetask.ru, @madetask.team, …
#   @made-task.com, @made-task.ru, …
#   @remozo.com, @remozo.ru, …
BLACKLIST_DOMAIN_PREFIXES: tuple[str, ...] = ("madetask.", "made-task.", "remozo.")

# Локальные префиксы, которые не могут принадлежать живому исполнителю.
BLACKLIST_LOCAL_PARTS: frozenset[str] = frozenset({
    "noreply",
    "no-reply",
    "postmaster",
    "mailer-daemon",
    "donotreply",
    "do-not-reply",
})


def _is_acceptable(email: str) -> bool:
    """True если email можно считать кандидатом на executor_email."""
    if "@" not in email:
        return False
    local, _, domain = email.rpartition("@")
    if domain in BLACKLIST_DOMAINS_EXACT:
        return False
    if any(domain.startswith(pfx) for pfx in BLACKLIST_DOMAIN_PREFIXES):
        return False
    if local in BLACKLIST_LOCAL_PARTS:
        return False
    # Дополнительная отсечка явно мусорных значений.
    if len(email) > 254:
        return False
    return True


def _extract_from_text(s: str | None) -> list[str]:
    if not s:
        return []
    s = _LITERAL_WHITESPACE_RE.sub(" ", s)
    return [m.group(0).lower() for m in EMAIL_RE.finditer(s)]


def _extract_from_json(s: str | None) -> list[str]:
    """Достаём email из items[].email — это поле приходит из ChatApp fromUser.email."""
    if not s:
        return []
    try:
        items = json.loads(s)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        e = it.get("email")
        if isinstance(e, str) and e.strip():
            out.append(e.strip().lower())
        # На всякий — поищем email и в тексте каждого сообщения, не только в плоском messages_text.
        for fld in ("text", "caption"):
            v = it.get(fld)
            if isinstance(v, str):
                out.extend(m.group(0).lower() for m in EMAIL_RE.finditer(v))
    return out


def pick_executor_email(candidates: Iterable[str]) -> str | None:
    """Принимаем сырой список email — возвращаем самый частый из allowed."""
    allowed = [e for e in candidates if _is_acceptable(e)]
    if not allowed:
        return None
    counts = Counter(allowed)
    # Counter.most_common стабилен по порядку первого появления при равенстве.
    return counts.most_common(1)[0][0]


def run(only_empty: bool, source_filter: str) -> None:
    where = []
    params: dict[str, object] = {}
    if only_empty:
        where.append("executor_email IS NULL")
    if source_filter != "all":
        where.append("source = :source")
        params["source"] = source_filter
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, source, messages_text, messages_json
            FROM dialogs
            {where_sql}
            ORDER BY id
        """), params).mappings().all()

    log.info("rows to scan: %d (only_empty=%s, source=%s)",
             len(rows), only_empty, source_filter)

    updated = 0
    matched_no_email = 0  # текст есть, но валидных email не нашли
    by_domain: Counter[str] = Counter()

    BATCH = 200
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        with engine.begin() as conn:
            for row in batch:
                candidates = (
                    _extract_from_text(row["messages_text"])
                    + _extract_from_json(row["messages_json"])
                )
                picked = pick_executor_email(candidates)
                if picked is None:
                    matched_no_email += 1
                    continue
                conn.execute(
                    text("UPDATE dialogs SET executor_email = :e WHERE id = :id"),
                    {"e": picked, "id": row["id"]},
                )
                updated += 1
                by_domain[picked.rpartition("@")[2]] += 1
        log.info("batch %d-%d done (updated=%d, no_email=%d)",
                 i, i + len(batch) - 1, updated, matched_no_email)

    log.info("=== Done: scanned=%d updated=%d no_email=%d ===",
             len(rows), updated, matched_no_email)
    if by_domain:
        log.info("Top-15 domains:")
        for dom, n in by_domain.most_common(15):
            log.info("  %-40s %d", dom, n)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only-empty", action="store_true", default=True,
                        help="Только строки с executor_email IS NULL (default: True)")
    parser.add_argument("--all-rows", dest="only_empty", action="store_false",
                        help="Перезаписать executor_email даже если уже заполнено")
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
