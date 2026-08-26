"""
Зеркало справочника компаний Superset (`mv.t_company_metadata_extended`).

Зачем. Названия компаний у нас и в Superset расходятся: мы собирали их из
названий чатов и причёсывали своим справочником синонимов. Сравнивать строки
дальше бессмысленно, любое новое имя чата снова разъедет. Поэтому сшиваем по
`company_id`, а название всегда берём из этого зеркала. Тогда оно совпадает с
аналитикой побуквенно, потому что источник один.

JOIN между нашей базой и складом Superset средствами SQL не сделать: это разные
СУБД, ходить туда можно только через API. Отсюда локальная копия.

Ключ составной: `platform` плюс `company_id`. Платформ три (RU, COM, Remozo),
нумерация компаний в каждой своя и начинается с единицы. Одного `company_id`
недостаточно, он склеит разные компании из разных платформ.

Справочник маленький (около 1500 строк), поэтому обновляем целиком, без
инкрементов. Идемпотентно: UPSERT по паре ключей.

Запуск:
  python -m sync_superset_companies
"""

from __future__ import annotations

import logging
import re
import sys

from sqlalchemy import text

from config import settings
from database import engine
from payouts_agent.superset_client import build_client_from_env


log = logging.getLogger(__name__)

SUPERSET_DATABASE_ID = 11
COMPANY_TABLE = "mv.t_company_metadata_extended"

# Организационно-правовые формы. Выкидываем при построении ключа: одна и та же
# компания в чате зовётся «Эктивейт», а в справочнике «ООО "ЭКТИВЕЙТ"».
_LEGAL_FORMS = (
    "ооо", "оао", "зао", "пао", "ао", "ип", "тоо", "чуп", "одо",
    "llc", "ltd", "inc", "corp", "gmbh", "sarl", "fzco", "fze",
    "limited", "company", "holding", "group", "sa", "sp", "zoo", "oy", "ab",
)
_LEGAL_RE = re.compile(r"\b(" + "|".join(_LEGAL_FORMS) + r")\b")
_QUOTES_RE = re.compile(r"[\"'«»“”„‘’`]")
_JUNK_RE = re.compile(r"[^0-9a-zа-яё]+")


def name_key(name: str | None) -> str:
    """Ключ для сопоставления названий, написанных по-разному.

    Схлопывает регистр, кавычки, форму собственности и всю пунктуацию.
    «ООО "ЭКТИВЕЙТ"» и «Эктивейт» дают одинаковый ключ.
    Ключ приблизительный, поэтому годится только как запасной путь: основной
    способ связи это company_id, полученный по почте.
    """
    s = (name or "").lower().replace("ё", "е")
    s = _QUOTES_RE.sub(" ", s)
    s = _LEGAL_RE.sub(" ", s)
    return _JUNK_RE.sub("", s)


DDL = """
CREATE TABLE IF NOT EXISTS superset_companies (
    platform       TEXT   NOT NULL,
    company_id     BIGINT NOT NULL,
    company_name   TEXT   NOT NULL,
    company_inn    TEXT,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, company_id)
);

CREATE TABLE IF NOT EXISTS superset_company_names (
    name_key     TEXT   NOT NULL,
    platform     TEXT   NOT NULL,
    company_id   BIGINT NOT NULL,
    company_name TEXT   NOT NULL,
    PRIMARY KEY (name_key, platform, company_id)
);
CREATE INDEX IF NOT EXISTS ix_superset_company_names_key
    ON superset_company_names (name_key);
"""


def fetch() -> list[dict]:
    client = build_client_from_env()
    client.authenticate()
    result = client.execute_sql(
        sql=(
            "SELECT DISTINCT platform, company_id, company_name, company_inn, last_task_date "
            f"FROM {COMPANY_TABLE} "
            "WHERE company_id IS NOT NULL AND company_name IS NOT NULL AND platform != ''"
        ),
        database_id=SUPERSET_DATABASE_ID,
        query_limit=100000,
        max_wait_seconds=180,
    )
    return result.get("rows") or []


def _entity_rank(row: dict) -> tuple:
    """Насколько запись годится в «лицо» компании. Больше — лучше.

    Показываем ту, по которой была последняя задача. 'NaT' значит задач не было
    вообще, 'Deleted' это надгробие удалённой компании.
    """
    name = (row.get("company_name") or "").strip()
    last = (row.get("last_task_date") or "").strip()
    has_date = last not in ("", "NaT", "None")
    return (name != "Deleted", has_date, last if has_date else "")


def run() -> int:
    rows = fetch()
    log.info("superset companies fetched: %d", len(rows))
    if not rows:
        log.warning("Справочник пуст, ничего не пишем: похоже на сбой доступа.")
        return 0

    # Группируем по ключу. Обычно строка одна, но подстраховываемся: у записи
    # может отличаться last_task_date, тогда берём свежую.
    by_company: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        by_company.setdefault((r["platform"], r["company_id"]), []).append(r)

    companies, variants = [], {}
    for (platform, cid), ents in by_company.items():
        best = max(ents, key=_entity_rank)
        inn = (best.get("company_inn") or "").strip()
        companies.append({
            "platform": platform,
            "company_id": cid,
            "company_name": (best["company_name"] or "").strip(),
            # В зарубежных юрлицах ИНН заполнен прочерком.
            "company_inn": inn if inn and inn != "-" else None,
        })
        # Все написания ведут на компанию: тикеты, у которых есть только имя из
        # чата, цепляются именно через них.
        for e in ents:
            key = name_key(e["company_name"])
            if key:
                variants[(key, platform, cid)] = (e["company_name"] or "").strip()

    with engine.begin() as conn:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))

        conn.execute(
            text("""
                INSERT INTO superset_companies
                       (platform, company_id, company_name, company_inn, synced_at)
                VALUES (:platform, :company_id, :company_name, :company_inn, now())
                ON CONFLICT (platform, company_id) DO UPDATE SET
                       company_name = EXCLUDED.company_name,
                       company_inn  = EXCLUDED.company_inn,
                       synced_at    = now()
            """),
            companies,
        )

        # Юрлицо могли переименовать или отвязать, поэтому список вариантов
        # перезаписываем целиком, а не доливаем.
        conn.execute(text("TRUNCATE superset_company_names"))
        conn.execute(
            text("""
                INSERT INTO superset_company_names
                       (name_key, platform, company_id, company_name)
                VALUES (:name_key, :platform, :company_id, :company_name)
            """),
            [{"name_key": k, "platform": p, "company_id": cid, "company_name": nm}
             for (k, p, cid), nm in variants.items()],
        )

    log.info("superset_companies upserted: %d, вариантов написания: %d",
             len(companies), len(variants))
    return len(companies)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
