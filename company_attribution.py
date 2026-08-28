"""
Company attribution daily job.

Цель — проставить диалогу компанию так, чтобы она сходилась с аналитикой.
Сходимость даёт не название, а company_id из Superset: названий у одной
компании бывает несколько, потому что бывает несколько юрлиц.

Шаги (порядок важен):
  1. sync_superset_companies.run     — обновляет локальное зеркало справочника
                                       компаний Superset и список написаний.
  2. extract_dialog_emails.run       — вытаскивает executor_email из messages_text
                                       / messages_json (regex + ChatApp fromUser.email).
  3. resolve_dialog_companies.run    — резолвит executor_email -> company + company_id
                                       через Superset CONTRACTOR MAIN.
                                       Fallback: COMPANY MAIN (customer_first_company_name).
                                       Внутренние компании (Apzone/Rosburn/Efficient)
                                       отфильтрованы.
  4. extract_company_from_group_name.run — для chat-name'ов вида «КЛИЕНТ × MT»
                                       (chatapp.chat_name / flomni incoming_messages.name)
                                       вытаскивает первый внешний токен и ПЕРЕЗАПИСЫВАЕТ
                                       company (короткая «человеческая» форма приоритетна
                                       над Superset).
  5. normalize_companies             — сводит синонимы названий к каноничным.
  6. match_companies_by_name         — добирает company_id тем, у кого осталось
                                       только название: почты в групповых чатах нет.

Все шаги идемпотентны: (2) и (3) ходят только по NULL'ам по умолчанию,
(4) перетирает Superset-значения только если имя группы их явно противоречит,
(6) трогает только строки без id.

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
import sync_superset_companies
from company_aliases import canonical_company
from config import settings
from database import engine
from sync_superset_companies import name_key


log = logging.getLogger(__name__)


def normalize_companies() -> None:
    """Свести синонимы dialogs.company к каноничному имени.

    Два шага. Сначала словарь company_aliases. Потом схлопывание написаний:
    «ООО "Потенциал"» и «ООО Потенциал» — одна компания, но отчёт группирует
    по строке названия и показывает их двумя строками. Одинаковыми считаем те,
    у которых совпадает name_key: он уже игнорирует регистр, кавычки и форму
    собственности. Из группы берём написание из справочника Superset, а если
    его там нет — самое частое. Ключ компании при этом не трогаем, меняется
    только отображаемое имя.

    Идёт по distinct-значениям и UPDATE'ит только те, что реально меняются,
    поэтому идемпотентно и дёшево."""
    with engine.connect() as conn:
        counts = {
            r[0]: r[1] for r in conn.execute(text(
                "SELECT company, count(*) FROM dialogs "
                "WHERE company IS NOT NULL GROUP BY company"
            ))
        }
        official = {
            r[0] for r in conn.execute(text(
                "SELECT DISTINCT company_name FROM superset_companies"
            ))
        }

    remap = {n: canonical_company(n) for n in counts}

    by_key: dict[str, list[str]] = {}
    for name in set(remap.values()):
        by_key.setdefault(name_key(name), []).append(name)

    for variants in by_key.values():
        if len(variants) < 2:
            continue
        # Частота считается по исходным написаниям, поэтому вариант, в который
        # уже свёл алиас, весит столько же, сколько его источники.
        weight = {v: sum(counts[o] for o, n in remap.items() if n == v) for v in variants}
        best = max(sorted(variants), key=lambda v: (v in official, weight[v]))
        for old, new in remap.items():
            if new in variants:
                remap[old] = best

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


def match_companies_by_name() -> None:
    """Добрать company_id там, где осталось одно название.

    Почта исполнителя есть не во всех диалогах: в групповых чатах компания
    взята из названия чата, связаться с Superset по почте нечем. Тогда идём
    от названия через ключ из sync_superset_companies.

    Ключ приблизительный, он схлопывает регистр, кавычки и форму собственности.
    Поэтому проставляем ключ только когда название ведёт ровно на одну компанию
    ровно одной платформы. Неоднозначные названия пропускаем: лучше пустое поле,
    чем чужая компания.

    Идём только по строкам без ключа, проставленное по почте не трогаем.
    """
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT company FROM dialogs "
            "WHERE company IS NOT NULL AND company_id IS NULL"
        ))]
        lookup = {
            r[0]: (r[1], r[2]) for r in conn.execute(text("""
                SELECT name_key, min(platform), min(company_id)
                  FROM superset_company_names
                 GROUP BY name_key
                HAVING count(DISTINCT (platform, company_id)) = 1
            """))
        }
        # Что почта уже сказала про это название. Если по одной части диалогов
        # компания известна точно, распространяем ключ на остальные. Так
        # разбираются неоднозначные названия вроде «Градус»: в справочнике их
        # четыре, но наши исполнители работают ровно с одним.
        known = conn.execute(text("""
            SELECT company, company_platform, company_id, count(*)
              FROM dialogs
             WHERE company IS NOT NULL AND company_id IS NOT NULL
             GROUP BY 1, 2, 3
        """)).all()

    seen: dict[str, dict[tuple[str, int], int]] = {}
    for company, platform, cid, n in known:
        hits = seen.setdefault(name_key(company), {})
        hits[(platform, cid)] = hits.get((platform, cid), 0) + n

    # Единичные попадания в чужую компанию бывают: исполнитель успел поработать
    # на нескольких заказчиков. Берём ответ, если он подавляющий. Если почта
    # разошлась всерьёз, название правда общее и гадать не надо.
    by_own = {}
    for key, hits in seen.items():
        (best, n), total = max(hits.items(), key=lambda kv: kv[1]), sum(hits.values())
        if n / total >= 0.8:
            by_own[key] = best

    matched = {}
    for n in names:
        key = name_key(n)
        hit = lookup.get(key) or by_own.get(key)
        if hit is not None:
            matched[n] = hit

    log.info("match_companies_by_name: %d названий без ключа, сопоставлено %d",
             len(names), len(matched))
    if not matched:
        return

    updated = 0
    with engine.begin() as conn:
        for name, (platform, cid) in matched.items():
            res = conn.execute(
                text("UPDATE dialogs SET company_id = :cid, company_platform = :plat "
                     "WHERE company = :name AND company_id IS NULL"),
                {"cid": cid, "plat": platform, "name": name},
            )
            updated += res.rowcount or 0
    log.info("match_companies_by_name: проставлен ключ у %d диалогов", updated)


def run() -> None:
    log.info("=== Company attribution pipeline START ===")

    log.info("--- step 1/6: sync_superset_companies ---")
    try:
        sync_superset_companies.run()
    except Exception:
        log.exception("sync_superset_companies failed")

    log.info("--- step 2/6: extract_dialog_emails ---")
    try:
        extract_dialog_emails.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("extract_dialog_emails failed")

    log.info("--- step 3/6: resolve_dialog_companies ---")
    try:
        resolve_dialog_companies.run(only_empty=True, source_filter="all")
    except Exception:
        log.exception("resolve_dialog_companies failed")

    log.info("--- step 4/6: extract_company_from_group_name ---")
    try:
        extract_company_from_group_name.run(dry_run=False)
    except Exception:
        log.exception("extract_company_from_group_name failed")

    log.info("--- step 5/6: normalize_companies ---")
    try:
        normalize_companies()
    except Exception:
        log.exception("normalize_companies failed")

    log.info("--- step 6/6: match_companies_by_name ---")
    try:
        match_companies_by_name()
    except Exception:
        log.exception("match_companies_by_name failed")

    log.info("=== Company attribution pipeline DONE ===")


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
