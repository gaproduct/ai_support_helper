"""
Каноникализация названий компаний.

Одна и та же компания приходит в разном написании из разных источников:
  - group-name чата:        «WowWorks», «Вауворкс Плюс»
  - Superset CONTRACTOR:    «ООО "Вауворкс Плюс"»
Чтобы в отчётах не дробить компанию на несколько строк, все варианты
сводятся к одному каноничному имени через ALIASES.

Механика:
  - ALIASES — список (pattern, canonical). pattern ищется без учёта регистра
    в нормализованном (схлопнуты пробелы, убраны кавычки/орг-формы) имени.
  - canonical_company(name) возвращает каноничное имя либо исходное (trimmed),
    если ни один alias не сработал.

Это ЕДИНСТВЕННОЕ место для добавления новых синонимов компаний.
"""

from __future__ import annotations

import re

# Орг-формы и кавычки, не несущие смысла для матчинга.
_ORG_FORMS = re.compile(
    r'\b(ооо|оао|зао|пао|ип|llc|ltd|inc|gmbh)\b|["«»“”\'`]',
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    s = _ORG_FORMS.sub(" ", name or "")
    return re.sub(r"\s+", " ", s).strip().lower()


# (substring-pattern в нормализованном имени) -> каноничное имя.
# Порядок важен: первый сработавший alias выигрывает.
ALIASES: tuple[tuple[str, str], ...] = (
    ("вауворкс", "WowWorks"),
    ("wowworks", "WowWorks"),
    ("градус", 'ООО "Градус"'),
)


def canonical_company(name: str | None) -> str | None:
    """Свести вариант написания компании к каноничному имени.

    Возвращает каноничное имя, если сработал alias; иначе — исходное имя
    с обрезанными пробелами (None остаётся None)."""
    if not name:
        return name
    norm = _normalize(name)
    for pattern, canonical in ALIASES:
        if pattern in norm:
            return canonical
    return name.strip()
