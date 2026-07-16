"""
compute_tickets.py — вычисляет тикеты по методологии D и записывает в таблицу tickets.

Запуск:
    python3 compute_tickets.py [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD] [--methodology D]

По умолчанию пересчитывает все классифицированные диалоги.

Методология D (вариант B):
- Диалог считается тикетом, если содержит хотя бы одно содержательное входящее
  сообщение ИЛИ категория KYC-bucket (порядок хранения сообщений не важен)
- executor-сторона: всегда 1 тикет на диалог
- customer-сторона: max(1, кол-во уникальных email исполнителей) тикетов на диалог
- Исключаются: рассылки про праздники/банковские выходные, внутренние чаты MadeTask

Кросс-кабинетные дубли (один Telegram-чат в двух Flomni-кабинетах) должны быть
удалены из таблицы dialogs до запуска этого скрипта.
"""

import argparse
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import text as sqlt

from database import get_session

log = logging.getLogger(__name__)

# ── Константы методологии ──────────────────────────────────────────────────

METHODOLOGY = "D"

KYC_CATEGORIES: frozenset[str] = frozenset({
    "KYC",
})

EXCLUDE_CATEGORIES: frozenset[str] = frozenset({"Тест", "Другое", "Рассылка", "Дубль"})

# Автоматические фиды/каналы (не человеческие обращения) — не тикеты.
# Напр. чат «Wallet XhPDT transactions» = системный лог USDT-транзакций.
EXCLUDE_CHATNAME_RE = re.compile(r"Wallet\s+\S+\s+transactions", re.I)

INTERNAL_CLIENT_PREFIXES: tuple[str, ...] = ("25ee7201", "ed3c0fcd")

BROADCAST_KEYWORDS: tuple[str, ...] = (
    "государственный выходной",
    "рекомендуем заранее пополнить",
    "нерабочие дни",
    "праздничные дни",
    "банковские выходные",
    "пополнение баланса недоступно",
    "работа в обычном режиме",
    "планируйте пополнение",
    "длинные выходные",
    # Массовые сервисные уведомления (рассылки менеджера в группах заказчиков)
    "переедет на новый адрес",
    "платформа скоро переедет",
    "переезжаем на новый адрес",
    "технические работы на стороне провайдеров",
    "ведутся технические работы на стороне",
)

# ── Служебный «шум»: диалоги, где inbound состоит только из /start, приветствий,
# благодарностей, эмодзи и т.п. — это не обращения, а не тикеты.
_AUTHOR_PREFIX_RE = re.compile(r"^@\S+[^:]*:\s*")
_NOISE_PHRASES: tuple[str, ...] = (
    "/start", "/silentclose", "/stop", "/silent",
    "доброго времени суток", "доброго дня", "добрый день", "доброе утро",
    "добрый вечер", "доброй ночи", "всем привет", "здравствуйте", "здравствуй",
    "приветствую", "привет", "hello", "hi ", "hey", "good morning",
    "большое спасибо", "спасибо большое", "спасибо за информацию", "спасибо",
    "благодарю", "спс", "thank you", "thanks", "пожалуйста",
    "хорошо", "понял", "поняла", "принято", "ок", "окей", "ok", "okay",
    "ага", "угу", "да", "нет",
)


def _strip_noise(text: str) -> str:
    """Убрать author-префикс и служебные слова; вернуть содержательный остаток."""
    t = _AUTHOR_PREFIX_RE.sub("", text.strip()).lower()
    for ph in _NOISE_PHRASES:
        t = t.replace(ph, " ")
    t = re.sub(r"[^\w]", " ", t, flags=re.UNICODE)  # эмодзи/пунктуация → пробел
    return t.strip()


def _is_service_noise(msgs: list[dict]) -> bool:
    """True если КАЖДОЕ inbound после чистки пустое (только /start/привет/спасибо/эмодзи)."""
    inbound = [
        m.get("text", "")
        for m in msgs
        if m.get("direction") == "inbound" and m.get("text", "").strip()
    ]
    if not inbound:
        return False  # случай «нет inbound» обрабатывает _is_broadcast
    return all(not _strip_noise(t) for t in inbound)

BLACKLIST_DOMAINS_EXACT: frozenset[str] = frozenset({"chatapp.online"})
BLACKLIST_DOMAIN_PREFIXES: tuple[str, ...] = ("madetask.", "made-task.", "remozo.")
BLACKLIST_LOCAL_PARTS: frozenset[str] = frozenset({
    "support", "info", "hello", "noreply", "no-reply", "admin",
})

_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[a-z]{2,}", re.IGNORECASE)


# ── Вспомогательные функции ────────────────────────────────────────────────

def _is_internal_email(email: str) -> bool:
    local, _, domain = email.lower().rpartition("@")
    if domain in BLACKLIST_DOMAINS_EXACT:
        return True
    if any(domain.startswith(p) for p in BLACKLIST_DOMAIN_PREFIXES):
        return True
    return local in BLACKLIST_LOCAL_PARTS


def _executor_emails(msgs: list[dict]) -> list[str]:
    """Уникальные внешние email-адреса исполнителей из текста диалога."""
    full_text = " ".join(m.get("text", "") for m in msgs)
    return sorted({
        e.lower()
        for e in _EMAIL_RE.findall(full_text)
        if not _is_internal_email(e)
    })


def _is_broadcast(msgs: list[dict]) -> bool:
    """True если все inbound-сообщения — рассылки про праздники/выходные."""
    inbound_texts = [
        m.get("text", "").lower()
        for m in msgs
        if m.get("direction") == "inbound" and m.get("text", "").strip()
    ]
    if not inbound_texts:
        return True  # нет inbound — нет реального обращения
    return all(
        any(kw in t for kw in BROADCAST_KEYWORDS)
        for t in inbound_texts
    )


def _qualifies_d(msgs: list[dict], category: str) -> bool:
    """Методология D (вариант B): диалог — тикет, если содержит хотя бы одно
    содержательное входящее сообщение ИЛИ относится к KYC-категории.

    Не зависит от порядка хранения сообщений (Flomni отдаёт историю новыми
    сверху, ChatApp — старыми сверху), поэтому проверяем наличие предметного
    inbound, а не msgs[0].
    """
    if not msgs:
        return False
    if category in KYC_CATEGORIES:
        return True
    return any(
        m.get("direction") == "inbound" and _strip_noise(m.get("text", ""))
        for m in msgs
    )


def _ticket_count(msgs: list[dict], side: str) -> int:
    if side == "executor":
        return 1
    emails = _executor_emails(msgs)
    return max(1, len(emails))


# ── Основная логика ────────────────────────────────────────────────────────

def compute(
    date_from: str | None = None,
    date_to: str | None = None,
    methodology: str = METHODOLOGY,
) -> int:
    """Пересчитывает тикеты и записывает в БД. Возвращает кол-во обработанных диалогов."""
    with get_session() as db:
        # Загружаем все классифицированные диалоги
        where_extra = ""
        params: dict = {"methodology": methodology}
        if date_from:
            where_extra += " AND d.dialog_date >= :date_from"
            params["date_from"] = date_from
        if date_to:
            where_extra += " AND d.dialog_date <= :date_to"
            params["date_to"] = date_to

        rows = db.execute(sqlt(f"""
            SELECT d.id, d.client_id, d.dialog_date, d.source, d.company,
                   d.messages_json, d.messages_count, d.chat_name,
                   ar.side, ar.category
            FROM dialogs d
            JOIN analysis_results ar ON ar.dialog_id = d.id
            WHERE d.processed = true
              AND ar.side IS NOT NULL
              AND ar.category IS NOT NULL
              {where_extra}
            ORDER BY ar.id DESC
        """), params).fetchall()

        # Дедупликация: один dialog_id → последний analysis_result
        seen_dialog: dict[int, object] = {}
        for r in rows:
            if r.id not in seen_dialog:
                seen_dialog[r.id] = r
        dialogs = list(seen_dialog.values())

        # Фильтр: внутренние чаты MadeTask
        dialogs = [
            d for d in dialogs
            if not any(d.client_id.startswith(p) for p in INTERNAL_CLIENT_PREFIXES)
        ]

        # Фильтр: категория «Тест»
        dialogs = [d for d in dialogs if d.category not in EXCLUDE_CATEGORIES]

        # Фильтр: автоматические фиды/каналы (напр. «Wallet ... transactions»)
        dialogs = [
            d for d in dialogs
            if not (d.chat_name and EXCLUDE_CHATNAME_RE.search(d.chat_name))
        ]

        # Дедупликация по (client_id, dialog_date) — берём запись с большим messages_count
        seen_key: dict[tuple, object] = {}
        for d in dialogs:
            key = (d.client_id, str(d.dialog_date))
            if key not in seen_key or (d.messages_count or 0) > (seen_key[key].messages_count or 0):
                seen_key[key] = d
        dialogs = list(seen_key.values())

        # Удаляем старые записи в диапазоне дат перед вставкой
        delete_params: dict = {"methodology": methodology}
        delete_where = "methodology = :methodology"
        if date_from:
            delete_where += " AND dialog_date >= :date_from"
            delete_params["date_from"] = date_from
        if date_to:
            delete_where += " AND dialog_date <= :date_to"
            delete_params["date_to"] = date_to
        db.execute(sqlt(f"DELETE FROM tickets WHERE {delete_where}"), delete_params)

        # Вычисляем и вставляем тикеты
        inserted = 0
        skipped_broadcast = 0
        skipped_method_d = 0
        skipped_noise = 0

        for d in dialogs:
            msgs: list[dict] = json.loads(d.messages_json or "[]")

            if _is_broadcast(msgs):
                skipped_broadcast += 1
                continue

            if _is_service_noise(msgs):
                skipped_noise += 1
                continue

            if not _qualifies_d(msgs, d.category):
                skipped_method_d += 1
                continue

            emails = _executor_emails(msgs)
            count = _ticket_count(msgs, d.side)

            db.execute(sqlt("""
                INSERT INTO tickets
                    (dialog_id, methodology, count, side, category, company,
                     dialog_date, source, executor_emails, computed_at)
                VALUES
                    (:dialog_id, :methodology, :count, :side, :category, :company,
                     :dialog_date, :source, CAST(:executor_emails AS jsonb), :computed_at)
                ON CONFLICT (dialog_id, methodology) DO UPDATE SET
                    count           = EXCLUDED.count,
                    side            = EXCLUDED.side,
                    category        = EXCLUDED.category,
                    company         = EXCLUDED.company,
                    dialog_date     = EXCLUDED.dialog_date,
                    source          = EXCLUDED.source,
                    executor_emails = EXCLUDED.executor_emails,
                    computed_at     = EXCLUDED.computed_at
            """), {
                "dialog_id":       d.id,
                "methodology":     methodology,
                "count":           count,
                "side":            d.side,
                "category":        d.category,
                "company":         d.company,
                "dialog_date":     d.dialog_date,
                "source":          d.source,
                "executor_emails": json.dumps(emails, ensure_ascii=False),
                "computed_at":     datetime.now(timezone.utc),
            })
            inserted += 1

        db.commit()

    log.info(
        "compute_tickets: inserted/updated=%d  skipped_broadcast=%d  skipped_noise=%d  skipped_method_d=%d",
        inserted, skipped_broadcast, skipped_noise, skipped_method_d,
    )
    return inserted


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Compute tickets and write to DB.")
    parser.add_argument("--date-from", default=None, help="YYYY-MM-DD")
    parser.add_argument("--date-to",   default=None, help="YYYY-MM-DD")
    parser.add_argument("--methodology", default=METHODOLOGY)
    args = parser.parse_args()

    n = compute(
        date_from=args.date_from,
        date_to=args.date_to,
        methodology=args.methodology,
    )
    print(f"Done: {n} ticket records written.")
