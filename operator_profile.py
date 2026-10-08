"""Профиль исполнителя для кабинета оператора.

Две задачи. Опознать человека по чату и достать его задачи, выплаты и заказчиков
из Superset.

Про опознание. Почту исполнителя проставляет ночная джоба extract_dialog_emails,
регуляркой по переписке. На новом обращении её ещё нет. Но client_id у человека
от обращения к обращению не меняется, поэтому берём самую свежую почту среди всех
диалогов этого чата. На замере это поднимает опознание с 44% до 70%. Если по
чату почты нет вообще, пробуем вытащить её из текущей переписки прямо сейчас.

Про Superset. Четыре запроса в SQL Lab отвечают за секунды, поэтому ответ кладём
в кэш на десять минут. Если Superset недоступен, возвращаем пустые блоки с текстом
ошибки: карточка должна открываться в любом случае.
"""
import logging
import re
import threading
import time
from typing import Any

from sqlalchemy import text

import extract_dialog_emails as ede
from payouts_agent.superset_client import build_client_from_env

log = logging.getLogger(__name__)

DATABASE_ID = 11
CACHE_TTL_SECONDS = 600
RECENT_LIMIT = 10
TASKS_LIMIT = 30  # активные все статусы плюс свежие закрытые

# Почта уходит в SQL текстом: SQL Lab не умеет связанные параметры. Поэтому
# пропускаем только заведомо безопасный вид адреса, всё остальное отбрасываем.
_SAFE_EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")

# Технические аккаунты Rosburn, Efficient и Apzone исключены во всех запросах,
# это стандартный фильтр обеих витрин.
_NOT_TECHNICAL = "coalesce(not_rosburn_efficient, 'Yes') = 'Yes'"

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_client = None
_client_lock = threading.Lock()


# ── Опознание ────────────────────────────────────────────────────────────────

def resolve_executor_email(db, source: str, client_id: str) -> tuple[str | None, str]:
    """Почта исполнителя по чату. Возвращает (почта, как нашли)."""
    row = db.execute(text("""
        SELECT executor_email FROM dialogs
        WHERE source = :src AND client_id = :cid AND executor_email IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """), {"src": source, "cid": client_id}).first()
    if row and row[0]:
        return row[0].strip().lower(), "по прошлым обращениям этого чата"

    # По чату почты не было. Пробуем найти её в том, что уже написано сегодня.
    row = db.execute(text("""
        SELECT messages_text, messages_json FROM dialogs
        WHERE source = :src AND client_id = :cid
        ORDER BY id DESC LIMIT 1
    """), {"src": source, "cid": client_id}).first()
    if not row:
        return None, "по чату истории нет"

    candidates = ede._extract_from_text(row[0]) + ede._extract_from_json(row[1])
    found = ede.pick_executor_email(candidates)
    if found:
        return found.strip().lower(), "из текста текущей переписки"
    return None, "почта в переписке не встречается"


# ── Superset ─────────────────────────────────────────────────────────────────

def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            client = build_client_from_env()
            client.authenticate()
            _client = client
        return _client


def _reset_client():
    global _client
    with _client_lock:
        _client = None


def _rows(sql: str) -> list[dict]:
    # Токен Superset протухает (истёк срок или Superset перезапустили),
    # тогда execute отвечает 401. Логинимся заново и повторяем один раз.
    try:
        result = _get_client().execute_sql(sql, database_id=DATABASE_ID, query_limit=1000)
    except Exception as exc:
        if "401" not in str(exc):
            raise
        log.warning("operator_profile: Superset 401, логинимся заново")
        _reset_client()
        result = _get_client().execute_sql(sql, database_id=DATABASE_ID, query_limit=1000)
    return [r for r in (result.get("rows") or []) if isinstance(r, dict)]


def _tasks_sql(email: str) -> str:
    # Сначала незакрытые задачи: у них closed_at пустой, и при сортировке только
    # по closed_at они уезжали в конец и срезались лимитом. Оператору же в
    # первую очередь нужны задачи в работе.
    return f"""
        SELECT id, name, status, closed_at, created_at,
               cost, currency, cost_in_rub, company_name,
               category_label_ru AS category, service_label
        FROM mv.t_tasks_extended
        WHERE lower(contractor_email) = '{email}' AND {_NOT_TECHNICAL}
        ORDER BY CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END DESC,
                 coalesce(closed_at, created_at) DESC
        LIMIT {TASKS_LIMIT}
    """


def _status_counts_sql(email: str) -> str:
    return f"""
        SELECT status, count() AS tasks
        FROM mv.t_tasks_extended
        WHERE lower(contractor_email) = '{email}' AND {_NOT_TECHNICAL}
        GROUP BY status
        ORDER BY tasks DESC
    """


def _payouts_sql(email: str) -> str:
    return f"""
        SELECT id, status, created_at, completed_at,
               amount, currency, amount_in_rub_cbr,
               payout_type, provider, payout_area, error_code, cancel_reason,
               tax_status, kyc_level, contractor_first_name, contractor_last_name,
               contractor_country_name, company_name, processing_time_in_days
        FROM mv.t_payout_extended
        WHERE lower(contractor_email) = '{email}' AND {_NOT_TECHNICAL}
        ORDER BY coalesce(completed_at, created_at) DESC
        LIMIT {RECENT_LIMIT}
    """


def _customers_sql(email: str) -> str:
    return f"""
        SELECT company_name,
               count() AS tasks,
               sum(cost_in_rub) AS rub,
               max(closed_at) AS last_task
        FROM mv.t_tasks_extended
        WHERE lower(contractor_email) = '{email}' AND {_NOT_TECHNICAL}
          AND company_name <> ''
        GROUP BY company_name
        ORDER BY last_task DESC
        LIMIT {RECENT_LIMIT}
    """


def _profile_from_payouts(payouts: list[dict]) -> dict[str, Any]:
    """Имя, налоговый статус и страна берутся из самой свежей выплаты."""
    if not payouts:
        return {}
    latest = payouts[0]
    name = " ".join(
        part for part in (latest.get("contractor_first_name"),
                          latest.get("contractor_last_name")) if part
    ).strip()
    return {
        "name": name or None,
        "tax_status": latest.get("tax_status"),
        "kyc_level": latest.get("kyc_level"),
        "country": latest.get("contractor_country_name"),
        "company": latest.get("company_name"),
    }


def profile(email: str) -> dict[str, Any]:
    """Задачи, выплаты и заказчики исполнителя. Никогда не бросает исключение."""
    email = (email or "").strip().lower()
    empty: dict[str, Any] = {
        "email": email, "tasks": [], "tasks_active": [], "tasks_by_status": [],
        "payouts": [], "customers": [], "profile": {}, "error": None,
    }
    if not _SAFE_EMAIL.match(email):
        empty["error"] = "адрес не похож на почту, запрос не отправлен"
        return empty

    with _cache_lock:
        hit = _cache.get(email)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    data = dict(empty)
    try:
        all_tasks = _rows(_tasks_sql(email))
        # Активные задачи показываем все, какие пришли; закрытые режем до
        # привычных десяти последних.
        data["tasks_active"] = [t for t in all_tasks if not t.get("closed_at")]
        data["tasks"] = [t for t in all_tasks if t.get("closed_at")][:RECENT_LIMIT]
        data["tasks_by_status"] = _rows(_status_counts_sql(email))
        data["payouts"] = _rows(_payouts_sql(email))
        data["customers"] = _rows(_customers_sql(email))
        data["profile"] = _profile_from_payouts(data["payouts"])
    except Exception as exc:
        log.error("operator_profile: Superset failed for %s: %s", email, exc)
        data["error"] = "Superset не ответил, данные по задачам и выплатам недоступны"
        return data  # неудачу не кэшируем, следующий заход попробует снова

    with _cache_lock:
        _cache[email] = (time.monotonic(), data)
    return data
