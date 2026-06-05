"""
Извлечение company из названия группы (вторая ветка атрибуции).

Источник: для chatapp — dialogs.chat_name, для flomni — incoming_messages.name
(JOIN по source='flomni' AND client_id).

Логика:
  1. Берём только названия, в которых ЯВНО есть внутренний маркер MT
     (madetask / remozo / mt / apzone / efficient / rosburn).
     Это сигнал, что чат вида «КЛИЕНТ × MT», а не персональный чат.
  2. Удаляем все внутренние подстроки.
  3. Сплитим по разделителям: & + | / \\ , _   и   ` - ` ` x ` ` Х `.
  4. Отбрасываем стоп-слова (вопросы/тех/test/onboarding/eor/...).
  5. Берём первый оставшийся токен — это и есть company.
  6. UPDATE dialogs.company БЕЗ ОГРАНИЧЕНИЯ company IS NULL — group-name
     побеждает Superset (короткая «человеческая» форма приоритетна).

Запуск:
  python -m extract_company_from_group_name [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter

from sqlalchemy import text

from core.config import settings
from core.database import engine


log = logging.getLogger(__name__)


INTERNAL_PATTERNS = (
    r"madetask", r"made\s*task", r"madetak",  # +typo «MadeTak»
    r"мэйдтаск", r"мейдтаск",                  # русская транскрипция
    r"\bmt\b",
    r"remozo", r"ремозо",
    r"apzone", r"апзон",
    r"efficient",
    r"rosburn",
    r"\beor\b",                                # маркер EOR-групп
)
_internal_re = re.compile("|".join(INTERNAL_PATTERNS), re.IGNORECASE)

SPLIT_RE = re.compile(r"[&+|/\\,]| +[-xXхХ] +|[_]", re.UNICODE)

# Слова, которые удаляются ПОСЛЕ internal-маркеров, но ДО split.
# Это «служебные» слова вокруг внутренних брендов (юр.оформление и т.п.),
# которые не несут информации о клиенте.
PRE_STRIP_RE = re.compile(r"\bкипр\b|\bинн\s*\d+\b", re.IGNORECASE)

STOPWORDS = (
    "вопросы", "по выплатам", "технические", "рабочий", "тестовый",
    "тест", "test", "for", "profit entry", "pe",
    "hr onboarding", "onboarding",
)


def has_internal_marker(s: str) -> bool:
    return bool(s) and _internal_re.search(s) is not None


def strip_internal(s: str) -> str:
    return _internal_re.sub("", s).strip()


def is_garbage(token: str) -> bool:
    t = token.strip().lower()
    if not t or len(t) < 2:
        return True
    if t in STOPWORDS:
        return True
    for sw in STOPWORDS:
        if t.startswith(sw):
            return True
    return False


def parse_group_name(name: str) -> str | None:
    if not has_internal_marker(name):
        return None
    cleaned = strip_internal(name)
    cleaned = PRE_STRIP_RE.sub("", cleaned).strip()
    # `EOR Кипр SoftGamings` после strip даёт «SoftGamings» без разделителей —
    # тоже валидный case. Не падаем на отсутствии split-токенов.
    tokens = SPLIT_RE.split(cleaned)
    tokens = [strip_internal(t).strip(" (),.-—<>\t") for t in tokens]
    tokens = [t for t in tokens if t and not is_garbage(t)]
    if not tokens:
        return None
    return tokens[0]


def run(dry_run: bool) -> None:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT 'chatapp' AS source, d.id, d.chat_name AS group_name, d.company
            FROM dialogs d
            WHERE d.source = 'chatapp' AND d.chat_name IS NOT NULL
            UNION ALL
            SELECT 'flomni' AS source, d.id, im.name AS group_name, d.company
            FROM dialogs d
            JOIN incoming_messages im
              ON im.source = 'flomni' AND im.client_id = d.client_id
            WHERE d.source = 'flomni' AND im.name IS NOT NULL AND im.name <> ''
        """)).all()

    log.info("rows with non-empty group_name: %d", len(rows))

    plan: dict[int, tuple[str, str, str | None]] = {}  # id -> (extracted, group_name, old_company)
    for source, did, group_name, company in rows:
        if did in plan:
            continue
        extracted = parse_group_name(group_name)
        if extracted is None:
            continue
        plan[did] = (extracted, group_name, company)

    log.info("dialogs to update: %d", len(plan))

    overwrites = sum(1 for _, _, old in plan.values() if old is not None and old.lower() != _.lower())
    fills = sum(1 for _, _, old in plan.values() if old is None)
    same = sum(1 for ext, _, old in plan.values() if old is not None and old.lower() == ext.lower())
    log.info("  fills (company was NULL): %d", fills)
    log.info("  overwrites (had Superset value): %d", overwrites)
    log.info("  same as Superset: %d", same)

    top = Counter(ext for ext, _, _ in plan.values()).most_common(15)
    log.info("Top-15 extracted companies:")
    for name, n in top:
        log.info("  %-50s %d", name, n)

    if dry_run:
        log.info("DRY RUN — no UPDATE performed.")
        return

    BATCH = 200
    items = list(plan.items())
    updated = 0
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        with engine.begin() as conn:
            for did, (ext, _, _) in chunk:
                res = conn.execute(
                    text("UPDATE dialogs SET company = :c WHERE id = :id"),
                    {"c": ext, "id": did},
                )
                updated += res.rowcount or 0
        log.info("batch %d-%d done (updated=%d)", i, i + len(chunk) - 1, updated)

    log.info("=== Done: dialog rows updated=%d ===", updated)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Только показать что будет — без UPDATE")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    main()
