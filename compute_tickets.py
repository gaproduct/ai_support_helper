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

Кросс-кабинетные дубли (один Telegram-чат в двух Flomni-кабинетах под разными
client_id) удаляются автоматически внутри compute() перед вставкой тикетов:
дубликат = совпадение содержания переписки при той же компании и дате; из пары
остаётся чат с групповым именем «…MadeTask». Ручная предварительная чистка
таблицы dialogs больше не требуется.
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

# Методология E: как D по фильтрам/квалификации диалогов, но customer-сторона
# считает тикеты по «группам»: несколько email в ОДНОМ сообщении = 1 тикет,
# разные email в РАЗНЫХ сообщениях = разные тикеты (см. _executor_email_groups).
METHODOLOGY = "E"

KYC_CATEGORIES: frozenset[str] = frozenset({
    "KYC",
})

# «Без запроса» — приветствие, /start, «спасибо», автозакрытие. Обращения как
# такового не было, поэтому такие диалоги не тикеты и статистику не искажают.
EXCLUDE_CATEGORIES: frozenset[str] = frozenset({
    "Тест", "Другое", "Рассылка", "Дубль", "Без запроса",
})

# Автоматические фиды/каналы (не человеческие обращения) — не тикеты.
# Напр. чат «Wallet XhPDT transactions» = системный лог USDT-транзакций.
EXCLUDE_CHATNAME_RE = re.compile(r"Wallet\s+\S+\s+transactions", re.I)

# fc1e4ebf = чат «Т-банк х МэйдТаск»: Т-банк — наш платёжный провайдер, не заказчик.
INTERNAL_CLIENT_PREFIXES: tuple[str, ...] = ("25ee7201", "ed3c0fcd", "fc1e4ebf")

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


def _executor_msg_emails(m: dict) -> set[str]:
    """Внешние email исполнителей внутри ОДНОГО сообщения."""
    return {
        e.lower()
        for e in _EMAIL_RE.findall(m.get("text", "") or "")
        if not _is_internal_email(e)
    }


def _executor_email_groups(msgs: list[dict]) -> int:
    """Число «групп» исполнителей (методология E).

    Считаем только по ВХОДЯЩИМ сообщениям: тикет заводит клиент, а не поддержка.
    Ответы поддержки игнорируются, иначе строка вида «Исполнители X и Y
    верифицированы» склеивала два независимых обращения в один тикет, а
    перечисление адресов в ответе плодило лишние.

    Правило: email'ы, перечисленные в ОДНОМ сообщении, — один тикет; email'ы,
    появившиеся в РАЗНЫХ сообщениях, — разные тикеты. Реализовано как число
    компонент связности графа, где вершины — email, а ребро соединяет адреса,
    упомянутые вместе в одном сообщении (один и тот же адрес в разных
    сообщениях остаётся одной вершиной, т.е. не даёт лишний тикет)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for m in msgs:
        if m.get("direction") != "inbound":
            continue
        emails = sorted(_executor_msg_emails(m))
        if not emails:
            continue
        for e in emails:
            find(e)
        for e in emails[1:]:
            union(emails[0], e)

    if not parent:
        return 0
    return len({find(e) for e in parent})


def _ticket_count(msgs: list[dict], side: str, methodology: str = METHODOLOGY) -> int:
    if side == "executor":
        return 1
    if methodology == "E":
        return max(1, _executor_email_groups(msgs))
    emails = _executor_emails(msgs)
    return max(1, len(emails))


# ── Кросс-кабинетная дедупликация Flomni ───────────────────────────────────
# Один и тот же групповой TG-чат приходит в двух Flomni-кабинетах (два бот-
# аккаунта) под разными client_id. Схлопывать такую пару ТОЛЬКО по «та же
# компания + день» нельзя — у одной компании легитимно бывает несколько разных
# чатов за день (разные исполнители/менеджеры), и это разные обращения.
#
# Надёжный признак дубля — совпадение СОДЕРЖАНИЯ переписки, а не поля company:
#   * у зеркал company-поле нередко расходится («ООО "Эктивейт"» vs «Эктивейт
#     ООО»), а в тексте перед каждым сообщением стоит разный «@автор:»-префикс —
#     поэтому сравниваем множества сообщений, очищенных от автор-префикса;
#   * одно зеркало обычно захватывает лишь часть сообщений другого, поэтому
#     критерий — ВЛОЖЕННОСТЬ (доля меньшего множества внутри большего), а не
#     Жаккар.
# Чтобы не склеить разные компании, случайно поделившиеся текстом (один менеджер
# в двух чатах), пара дополнительно проверяется на совместимость по токенам
# company-поля. Из пары оставляем чат с групповым именем «…MadeTask», иначе — с
# большим messages_count.

_AUTHOR_MSG_PREFIX_RE = re.compile(r"^@\S+[^:]*:\s*")
# Юрформы и служебные токены, не различающие компанию.
_COMPANY_STOPWORDS: frozenset[str] = frozenset({
    "ооо", "оао", "зао", "пао", "ао", "ип", "llc", "ltd", "inc", "sro",
    "made", "task", "madetask", "company",
})
_COMPANY_TOKEN_RE = re.compile(r"[0-9a-zа-яё]{2,}", re.IGNORECASE)

# Дубль: не менее _DEDUP_MIN_SHARED общих содержательных сообщений, у обоих
# диалогов их не менее _DEDUP_MIN_SIZE, и вложенность меньшего в больший
# не ниже _DEDUP_MIN_CONTAINMENT.
_DEDUP_MIN_SHARED = 4
_DEDUP_MIN_SIZE = 4
_DEDUP_MIN_CONTAINMENT = 0.9


def _is_group_chat(chat_name: str | None) -> bool:
    return bool(chat_name and "madetask" in chat_name.lower().replace(" ", ""))


def _content_signature(msgs: list[dict]) -> frozenset[str]:
    """Множество содержательных сообщений (len>=12) без «@автор:»-префикса."""
    out: set[str] = set()
    for m in msgs:
        txt = _AUTHOR_MSG_PREFIX_RE.sub("", (m.get("text") or "").strip())
        txt = re.sub(r"\s+", " ", txt).lower().strip()
        if len(txt) >= 12:
            out.add(txt)
    return frozenset(out)


def _company_tokens(company: str | None) -> frozenset[str]:
    return frozenset(
        _COMPANY_TOKEN_RE.findall((company or "").lower())
    ) - _COMPANY_STOPWORDS


def _company_compatible(a, b) -> bool:
    """Совместимы, если у одной из сторон company не резолвится, либо токены
    company-поля пересекаются. Отсекает склейку разных компаний с общим текстом."""
    ta, tb = _company_tokens(a.company), _company_tokens(b.company)
    if not ta or not tb:
        return True
    return bool(ta & tb)


def _dedupe_cross_cabinet(dialogs: list) -> tuple[list, int]:
    """Удаляет кросс-кабинетные Flomni-дубли (совпадение контента при совместимой
    компании, в тот же день). Возвращает (оставшиеся_диалоги, кол-во_удалённых).
    Не-Flomni диалоги проходят насквозь без изменений."""
    by_date: dict[str, list] = {}
    passthrough: list = []
    for d in dialogs:
        if d.source != "flomni":
            passthrough.append(d)
            continue
        by_date.setdefault(str(d.dialog_date), []).append(d)

    kept_all: list = []
    dropped = 0
    for members in by_date.values():
        if len(members) == 1:
            kept_all.append(members[0])
            continue
        sigs = {
            d.id: _content_signature(json.loads(d.messages_json or "[]"))
            for d in members
        }
        # Приоритет сохранения: групповое имя «…MadeTask», затем больший объём.
        # Объём берём из сигнатуры, а не из messages_count: у flomni-диалогов
        # это поле пустое, и тогда пара разрешалась порядком строк. Терялся тот
        # диалог, где резолвится компания. Последним идёт id — чтобы результат
        # не зависел от порядка выборки.
        ordered = sorted(
            members,
            key=lambda x: (_is_group_chat(x.chat_name), len(sigs[x.id]), x.id),
            reverse=True,
        )
        kept: list = []
        for d in ordered:
            sd = sigs[d.id]
            is_dup = False
            for k in kept:
                sk = sigs[k.id]
                inter = len(sd & sk)
                if (
                    inter >= _DEDUP_MIN_SHARED
                    and len(sd) >= _DEDUP_MIN_SIZE
                    and len(sk) >= _DEDUP_MIN_SIZE
                    and inter / min(len(sd), len(sk)) >= _DEDUP_MIN_CONTAINMENT
                    and _company_compatible(d, k)
                ):
                    is_dup = True
                    break
            if is_dup:
                dropped += 1
            else:
                kept.append(d)
        kept_all.extend(kept)

    return passthrough + kept_all, dropped


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

        # Кросс-кабинетная дедупликация Flomni: один групповой TG-чат из двух
        # кабинетов (разные client_id) = дубликат при совпадении контента + той
        # же компании и дате. Раньше это делалось вручную до запуска скрипта;
        # теперь выполняется автоматически, чтобы дубли не попадали в отчёт.
        dialogs, dropped_cross_cabinet = _dedupe_cross_cabinet(dialogs)

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
            count = _ticket_count(msgs, d.side, methodology)

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
        "compute_tickets: inserted/updated=%d  skipped_broadcast=%d  skipped_noise=%d  "
        "skipped_method_d=%d  dropped_cross_cabinet=%d",
        inserted, skipped_broadcast, skipped_noise, skipped_method_d, dropped_cross_cabinet,
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
