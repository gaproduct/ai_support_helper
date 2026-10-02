"""
report_filters.py — общие константы и функции фильтрации для построения отчётов.

Реализует методологию «D» (METHODOLOGY.md §1.3–§1.6):
  - Исключение внутренних клиентских ID и broadcast-рассылок.
  - Фильтрация по первому сообщению (inbound-first или KYC-outreach).
  - Дедупликация кросс-кабинетных дублей.
  - Подсчёт тикетов: 1 для executor-side, max(1, уник. email) для customer-side.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Iterable

from sqlalchemy import text

log = logging.getLogger(__name__)


# ─────────────────────── Внутренние клиентские ID ────────────────────────────

# Два командных Group-чата MadeTask, которые Flomni индексирует как клиентов.
# Хранятся как prefix-строки UUID (полные значения см. в БД — поле client_id).
# Обновите при появлении новых внутренних чатов.
INTERNAL_CLIENT_IDS: frozenset[str] = frozenset({
    "25ee7201",  # внутренний группочат MT #1 — указан prefix UUID
    "ed3c0fcd",  # внутренний группочат MT #2 — указан prefix UUID
    "fc1e4ebf",  # чат «Т-банк х МэйдТаск» — Т-банк наш платёжный провайдер, не заказчик
})


def is_internal_client(client_id: str) -> bool:
    """True если client_id относится к внутреннему чату команды."""
    return any(client_id.startswith(pfx) for pfx in INTERNAL_CLIENT_IDS)


# ─────────────────────── Broadcast-паттерны ──────────────────────────────────

# Регулярки для обнаружения информационных рассылок оператора (не тикеты).
# Типовые примеры: уведомления о графике работы банков в праздники.
BROADCAST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"график\s+работы\s+банк", re.IGNORECASE),
    re.compile(r"в\s+связи\s+с\s+праздник", re.IGNORECASE),
    re.compile(r"нерабочи[йе]\s+дн", re.IGNORECASE),
    re.compile(r"день\s+Росси", re.IGNORECASE),
    re.compile(r"12\s+июня", re.IGNORECASE),
    re.compile(r"выходн[ыой]+\s+дн", re.IGNORECASE),
    re.compile(r"банки?\s+(не\s+)?работа[юет]", re.IGNORECASE),
)


def is_broadcast(dialog: dict) -> bool:
    """True если все сообщения диалога соответствуют шаблонам broadcast-рассылки.

    Проверяет текст первого outbound-сообщения и отсутствие содержательного
    inbound-ответа клиента.
    """
    msgs = _load_messages(dialog)
    if not msgs:
        return False

    # Ищем broadcast-маркеры в outbound-текстах
    outbound_texts = [
        m.get("text", "") for m in msgs if m.get("direction") == "outbound"
    ]
    has_broadcast_outbound = any(
        any(pat.search(t) for pat in BROADCAST_PATTERNS)
        for t in outbound_texts
        if t
    )
    if not has_broadcast_outbound:
        return False

    # Если есть содержательный inbound — не broadcast, а реальный диалог
    inbound_texts = [
        m.get("text", "").strip()
        for m in msgs if m.get("direction") == "inbound"
    ]
    has_meaningful_inbound = any(len(t) > 10 for t in inbound_texts)
    return not has_meaningful_inbound


# ─────────────────────── KYC-категории ───────────────────────────────────────

# Категории, которые считаются «KYC-outreach» при методологии D:
# outbound-инициированные диалоги по этим категориям — рабочие тикеты.
KYC_CATEGORIES: frozenset[str] = frozenset({
    "KYC",
    "Проблема KYC",
    "Дублирование KYC",
})


# ─────────────────────── Фильтрация email ────────────────────────────────────

# Префиксы доменов внутренних брендов — дублируют extract_dialog_emails.py,
# чтобы report_filters оставался самодостаточным модулем.
_INTERNAL_DOMAIN_PREFIXES: tuple[str, ...] = ("madetask.", "made-task.", "remozo.")
_INTERNAL_DOMAINS_EXACT: frozenset[str] = frozenset({"chatapp.online"})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LITERAL_WS_RE = re.compile(r"\\{1,2}[nrt]")


def _is_internal_email(email: str) -> bool:
    """True если email принадлежит внутренней стороне (не исполнителю)."""
    if "@" not in email:
        return True
    _, _, domain = email.rpartition("@")
    if domain in _INTERNAL_DOMAINS_EXACT:
        return True
    return any(domain.startswith(pfx) for pfx in _INTERNAL_DOMAIN_PREFIXES)


def _extract_executor_emails(dialog: dict) -> set[str]:
    """Извлечь уникальные внешние email исполнителей из сообщений диалога."""
    raw = dialog.get("messages_json") or dialog.get("messages_text") or ""
    cleaned = _LITERAL_WS_RE.sub(" ", raw)
    emails = {m.group(0).lower() for m in _EMAIL_RE.finditer(cleaned)}

    # Дополнительно: structured email из messages_json
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    e = it.get("email")
                    if isinstance(e, str) and e.strip():
                        emails.add(e.strip().lower())
    except (json.JSONDecodeError, TypeError):
        pass

    return {e for e in emails if not _is_internal_email(e)}


# ─────────────────────── Вспомогательные утилиты ─────────────────────────────

def _load_messages(dialog: dict) -> list[dict]:
    raw = dialog.get("messages_json") or dialog.get("messages_text") or ""
    try:
        msgs = json.loads(raw)
        return msgs if isinstance(msgs, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _outbound_count(dialog: dict) -> int:
    return sum(
        1 for m in _load_messages(dialog)
        if isinstance(m, dict) and m.get("direction") == "outbound"
    )


# ─────────────────────── Методология D ───────────────────────────────────────

def apply_methodology_d(dialogs: Iterable[dict]) -> list[dict]:
    """Отфильтровать диалоги по методологии D (METHODOLOGY.md §1.3).

    В тикеты попадают только:
      1. Диалоги, в которых первое сообщение — inbound (клиент обратился сам).
      2. Диалоги, в которых первое сообщение — outbound, но категория = KYC
         (это активная работа поддержки по верификации, не follow-up).

    Остальные outbound-инициированные диалоги не считаются тикетами.

    Args:
        dialogs: итерируемый набор dict с ключами:
            messages_json / messages_text (JSON-массив сообщений),
            category (str, результат AI-классификации).
    Returns:
        Отфильтрованный список dict.
    """
    all_dialogs = list(dialogs)
    result: list[dict] = []

    for d in all_dialogs:
        msgs = _load_messages(d)
        if not msgs:
            log.debug("apply_methodology_d: dialog %s has no messages — skipped.", d.get("id"))
            continue

        first_direction = msgs[0].get("direction", "")

        if first_direction == "inbound":
            result.append(d)
        elif first_direction == "outbound" and d.get("category") in KYC_CATEGORIES:
            result.append(d)
        else:
            log.debug(
                "apply_methodology_d: dialog %s excluded "
                "(first=%s, category=%s).",
                d.get("id"), first_direction, d.get("category"),
            )

    log.info(
        "apply_methodology_d: %d → %d dialogs after D-filter.",
        len(all_dialogs), len(result),
    )
    return result


# ─────────────────────── Дедупликация кросс-кабинетных дублей ────────────────

def dedup_cross_cabinet(dialogs: Iterable[dict]) -> list[dict]:
    """Удалить кросс-кабинетные дубликаты (METHODOLOGY.md §1.4).

    «Кросс-кабинетный дубль»: один и тот же Telegram-чат (или исполнитель)
    проиндексирован Flomni как два разных client_id из разных кабинетов.
    Критерий группировки: (executor_email, дата started_at[:10]).
    Из каждой группы оставляем диалог с наибольшим числом outbound-сообщений
    (больше операторской активности = «основной» кабинет).
    Диалоги без executor_email не дедуплицируются (оставляем все).

    Args:
        dialogs: итерируемый набор dict с ключами:
            executor_email (str | None),
            started_at (str ISO, первые 10 символов — дата),
            messages_json / messages_text.
    Returns:
        Список dict без кросс-кабинетных дублей.
    """
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    no_key: list[dict] = []

    for d in dialogs:
        email = (d.get("executor_email") or "").strip().lower()
        date = (d.get("started_at") or "")[:10]
        if email and date:
            grouped[(email, date)].append(d)
        else:
            no_key.append(d)

    result: list[dict] = list(no_key)
    removed = 0

    for (email, date), group in grouped.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            winner = max(group, key=_outbound_count)
            result.append(winner)
            removed += len(group) - 1
            log.debug(
                "dedup_cross_cabinet: kept dialog %s for (%s, %s), "
                "dropped %d duplicate(s).",
                winner.get("id"), email, date, len(group) - 1,
            )

    if removed:
        log.info("dedup_cross_cabinet: removed %d cross-cabinet duplicate(s).", removed)

    return result


# ─────────────────────── Подсчёт тикетов ─────────────────────────────────────

def count_tickets(dialog: dict, side: str) -> int:
    """Количество тикетов для одного диалога (METHODOLOGY.md §2, Шаг 4).

    Правила:
      executor-side: всегда 1 тикет (диалог 1×1 с конкретным исполнителем).
      customer-side: max(1, число уникальных внешних email исполнителей
                     в переписке). Внутренние домены (@madetask.*, @remozo.*)
                     не учитываются.

    Обоснование: менеджер заказчика может в одном чате написать про
    нескольких разных исполнителей — каждый email = отдельный кейс.

    Args:
        dialog: dict с ключами messages_json / messages_text.
        side:   "customer" | "executor" (из поля analysis_results.side).
    Returns:
        Целое число тикетов >= 1.
    """
    if side == "executor":
        return 1

    # customer-side: считаем уникальные внешние email исполнителей
    executor_emails = _extract_executor_emails(dialog)
    return max(1, len(executor_emails))


# ─────────────────────── Агрегированная статистика ───────────────────────────

_STATS_SQL = """
SELECT
    COALESCE(LEFT(d.started_at, 10), d.dialog_date::text) AS day,
    ar.side,
    ar.category,
    d.messages_text,
    d.messages_json
FROM dialogs d
JOIN analysis_results ar ON ar.dialog_id = d.id
WHERE COALESCE(LEFT(d.started_at, 10), d.dialog_date::text) BETWEEN :date_from AND :date_to
"""


def compute_daily_stats(
    date_from: str,
    date_to: str,
    exclude_categories: frozenset[str] = frozenset({"Другое", "Тест"}),
    methodology_d: bool = False,
) -> dict[str, dict[str, int]]:
    """Агрегировать тикеты по (день, категория) с учётом правила customer multi-email.

    Правило: для customer-side каждый уникальный email исполнителя в переписке
    считается отдельным тикетом (max(1, len(unique_executor_emails))).
    Для executor-side всегда 1 тикет.

    Это единственная корректная функция для подсчёта тикетов в отчётах.
    НЕ использовать COUNT(*) из SQL напрямую — это считает диалоги, не тикеты.

    Args:
        date_from: начало периода включительно (YYYY-MM-DD).
        date_to:   конец периода включительно (YYYY-MM-DD).
        exclude_categories: категории, исключаемые из результата.
        methodology_d: применить apply_methodology_d() перед подсчётом.

    Returns:
        {day_str: {category: ticket_count}}
    """
    from database import engine  # local import to avoid circular dependency at module load

    with engine.connect() as conn:
        rows = conn.execute(
            text(_STATS_SQL), {"date_from": date_from, "date_to": date_to}
        ).mappings().all()

    dialogs = [dict(r) for r in rows]

    if methodology_d:
        dialogs = apply_methodology_d(dialogs)

    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for d in dialogs:
        day = d.get("day") or ""
        category = d.get("category") or "Другое"
        side = d.get("side") or "executor"

        if category in exclude_categories:
            continue

        result[day][category] += count_tickets(d, side)

    log.info(
        "compute_daily_stats: %s–%s → %d dialogs, %d days.",
        date_from, date_to, len(dialogs), len(result),
    )
    return dict(result)
