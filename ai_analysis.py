"""
AI analysis worker — runs every 24 hours.

Унифицированная классификация по методологии май-отчёта:
  1. LLM получает переписку и выбирает СТОРОНУ (customer / executor) и
     одну категорию из канонического списка для этой стороны.
  2. «Потенциальный клиент» — обычное значение category (executor-сторона
     для пользователей, которые ещё не работают с сервисом и интересуются
     условиями).
  3. Дополнительно возвращаются summary / sentiment / priority / resolution.

Категориальные списки — те же, что использовались для отчёта за май в
build_reports_v3.py (CATEGORIES_CUST / CATEGORIES_EXEC).
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from config import settings
from database import AnalysisResult, Dialog, get_session


log = logging.getLogger(__name__)

client = OpenAI(api_key=settings.openai_api_key)


# ─────────────────────────── Каноничные категории ───────────────────────────
# Должны быть синхронны с build_reports_v3.py. «Потенциальный клиент»
# присутствует в executor-списке (исторически новые компании заходят со
# стороны исполнителя в очередь поддержки).

CATEGORIES_CUSTOMER: tuple[str, ...] = (
    "KYC",
    "Выплаты и проблемы с ними",
    "Техническая проблема/вопрос",
    "Запрос документов",
    "Зачисление платежа на баланс",
    "Вопросы по числам отправки закрывашек в эдо заказчику/поторопить бухгалтерию",
    "Вопросы по работе в сервисе",
    "Функциональность сервиса и возможность выплат",
    "Изменения в профиле заказчика/исполнителя",
    "Налоговый статус исполнителя",
    "Потенциальный клиент",
    "Без запроса",
    "Другое",
)

CATEGORIES_EXECUTOR: tuple[str, ...] = (
    "Без запроса",
    "Другое",
    "Выплаты и проблемы с ними",
    "KYC",
    "ИП РФ",
    "SEPA/SWIFT",
    "Изменение/удаление аккаунта",
    "Курсы/комиссии/лимиты",
    "Функциональность сервиса и возможность выплат",
    "Зачисление платежа на баланс",
    "Техническая проблема/вопрос",
    "Статус ИП РФ/Самозанятого",
    "Запрос документов",
    "Потенциальный клиент",
    "НДФЛ",
    "Тест",
    "Реквизиты заблокированы",
    "Предложение (маркетинг, сотрудничество, банкинг)",
    "EOR",
    "Вопросы по работе в сервисе",
)

POTENTIAL_CUSTOMER_CATEGORY = "Потенциальный клиент"


# ─────────────────────────── Подкатегории ───────────────────────────
# Детализация ВНУТРИ основной category. Заполняется только для категорий,
# перечисленных здесь; для всех остальных subcategory = None.
# Одинаковы для обеих сторон (side) — суть подкатегории не зависит от того,
# заказчик пишет или исполнитель.

SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Выплаты и проблемы с ними": (
        "Зависание в обработке",
        "Уточнение причин отказа по платежу",
        "Изменение реквизитов",
        "Комиссия/курс/лимиты",
        "Документы по выплате",
    ),
    "KYC": (
        "Запрос на верификацию",
        "Уточнение причины отказа",
        "Зависание верификации",
        "Повторная/сброс верификации",
        "Ошибка прохождения",
        "Дубликат/объединение профиля",
    ),
    # «Запрос документов» — единая категория для обеих сторон (кто запросил —
    # различается полем side). Тип запрашиваемого документа side-agnostic.
    # Платёжные документы (инвойс/платёжка/receipt/выписка о выплатах) сюда НЕ относятся —
    # они идут в «Выплаты и проблемы с ними» → «Документы по выплате».
    # «Закрывающие/ЭДО» — запрос/получение/статус закрывающих документов
    #   (акт/УПД/счёт-фактура): «пришлите акты», «где закрывашки», «статус в ЭДО».
    # «Отправка документов в ЭДО» — активная передача/выставление документов в ЭДО
    #   и подключение к нему: «отправьте документы в ЭДО», «выставите УПД»,
    #   «направьте акт», приглашение/подключение контрагента к ЭДО.
    "Запрос документов": (
        "Закрывающие/ЭДО",
        "Отправка документов в ЭДО",
        "Договор/оферта",
        "Документы для визы/ВНЖ/банка",
        "Реквизиты/данные организации",
    ),
}


def _format_list(items: tuple[str, ...]) -> str:
    return "\n".join(f"  - {x}" for x in items)


def _format_subcategories(subs: dict[str, tuple[str, ...]]) -> str:
    blocks = []
    for cat, items in subs.items():
        blocks.append(f"  «{cat}»:\n" + "\n".join(f"    - {x}" for x in items))
    return "\n".join(blocks)


# ── Rules loader ─────────────────────────────────────────────────────────────

_RULES_PATH = Path(__file__).parent / "CLASSIFICATION_RULES.md"
_RULES_START = "<!-- RULES_START -->"
_RULES_END = "<!-- RULES_END -->"


def _load_rules() -> str:
    """Extract §3–§10 from CLASSIFICATION_RULES.md (between HTML comment markers).

    Falls back to the full file if markers are missing, logging a warning.
    Called once at module load — edits to the markdown take effect on restart.
    """
    try:
        text = _RULES_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Cannot read %s: %s — using empty rules.", _RULES_PATH, exc)
        return ""

    start = text.find(_RULES_START)
    end = text.find(_RULES_END)
    if start == -1 or end == -1:
        log.warning(
            "%s missing RULES_START/END markers — loading full file.", _RULES_PATH.name
        )
        return text.strip()

    return text[start + len(_RULES_START):end].strip()


# ── System prompt ─────────────────────────────────────────────────────────────
# Preamble (in code): role, JSON output contract, side definition, categories.
# Rules (from markdown §3–§10): KYC priority, disambiguation, markers, outbound.
# Kept separate so rules can be edited in CLASSIFICATION_RULES.md without
# touching Python code.

_PROMPT_PREAMBLE = """\
Ты аналитик службы поддержки B2B-платёжного сервиса.
Проанализируй переписку и верни ТОЛЬКО валидный JSON-объект (без markdown, без комментариев).

ПОЛЯ JSON:

  "side"       — ЗАКАЗЧИК или ИСПОЛНИТЕЛЬ — определяется по СОДЕРЖАНИЮ inbound, не по форме.
                 "executor" = ИСПОЛНИТЕЛЬ / КОНТРАКТОР (физлицо / ИП, которое ПОЛУЧАЕТ выплаты).
                   Маркеры: первое лицо про СВОЁ — «я не могу пройти KYC», «не пришла МОЯ
                   выплата», «нажимаю вывод, мне приходит код», «МОЙ статус / кабинет / ИП»,
                   «my withdrawal». Сюда же — незарегистрированные пользователи и
                   потенциальные новые исполнители.
                 "customer" = ЗАКАЗЧИК-ПЛАТЕЛЬЩИК (компания / ИП, которая ПЛАТИТ исполнителям).
                   Маркеры: пишет про ДРУГОГО человека — «верифицируйте НАШЕГО исполнителя»,
                   «выплата НАШЕМУ фрилансеру», называет ФИО / email / id третьего лица,
                   говорит от лица бизнеса: «у нас», «наша компания», «наши исполнители».
                 ПРИОРИТЕТНОЕ ПРАВИЛО: если автор пишет от ПЕРВОГО ЛИЦА про получение СВОЕЙ
                   выплаты / прохождение СВОЕГО KYC / СВОЙ налоговый статус / СВОЙ аккаунт —
                   это "executor", ДАЖЕ если его можно назвать «клиентом сервиса».
                   customer — ТОЛЬКО тот, кто ПЛАТИТ исполнителям.
                 ВАЖНО ПРО ФОРМАТ: ОДИН и тот же префикс «@username Имя:» в начале реплик —
                   это просто автор сообщения (Flomni / Telegram подставляет отправителя).
                   Один автор САМ ПО СЕБЕ НЕ делает диалог групповым и НЕ означает customer —
                   ориентируйся на СУТЬ: про чьи деньги / KYC / аккаунт идёт речь.
                 ГРУППОВОЙ ЧАТ = ВСЕГДА customer: если в inbound пишут ДВА И БОЛЕЕ РАЗНЫХ
                   автора («@M_Gupalo …», «@eternityviki …», «@Cerber650 …»), это корпоративный
                   чат заказчика, где сотрудники компании ведут обращения за СВОИХ исполнителей —
                   часто сразу по нескольким, с разными email вида performer.*@… . Такой диалог
                   всегда customer, даже если системный текст процитирован во втором лице
                   («Вас не удалось верифицировать… ваш профиль»).

  "category"   — ровно одна из категорий для данного side (см. списки ниже).

  "subcategory"— уточнение ВНУТРИ category. Заполняй ТОЛЬКО если выбранная
                 category присутствует в блоке ПОДКАТЕГОРИИ ниже; тогда выбери
                 ровно одну подкатегорию из её списка. Если у category нет
                 подкатегорий — верни пустую строку "".

  "summary"    — краткое содержание диалога (1–3 предложения на языке оригинала).
  "sentiment"  — тональность клиента: "positive" | "neutral" | "negative".
  "priority"   — приоритет тикета: "low" | "medium" | "high" | "critical".
  "resolution" — итог диалога: "resolved" | "unresolved" | "escalated".
  "rationale"  — ОБЯЗАТЕЛЬНО. Формат: «<категория> — <прямая цитата из inbound>».
                 Цитата — реальная фраза из сообщения клиента, не выдуманная.
                 Примеры:
                   «Выплаты и проблемы с ними — "не пришли деньги на грузинскую карту".»
                   «Запрос документов — "копию оригинала контракта с апостилем".»
                   «Техническая проблема/вопрос — "не могу вспомнить пароль".»
                   «KYC — "помогите верифицировать исполнителя".»
                   «Без запроса — нет содержательного запроса ("/start").»

КАТЕГОРИИ (side=customer):
{categories_customer}

КАТЕГОРИИ (side=executor):
{categories_executor}

ПОДКАТЕГОРИИ (заполняй subcategory только для этих категорий):
{subcategories}

ПРАВИЛА КЛАССИФИКАЦИИ:
"""

_PROMPT_SUFFIX = "\n\nНе добавляй никакого текста вне JSON-объекта."


def _build_system_prompt() -> str:
    preamble = _PROMPT_PREAMBLE.format(
        categories_customer=_format_list(CATEGORIES_CUSTOMER),
        categories_executor=_format_list(CATEGORIES_EXECUTOR),
        subcategories=_format_subcategories(SUBCATEGORIES),
    )
    return preamble + _load_rules() + _PROMPT_SUFFIX


SYSTEM_PROMPT = _build_system_prompt()


USER_PROMPT_TEMPLATE = """Проанализируй следующую переписку поддержки:

{dialog_text}"""


def _call_openai(dialog_text: str) -> tuple[dict | None, str]:
    """Send dialog text to OpenAI and return (parsed_dict, raw_json_string).
    Returns (None, "") on error."""
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(dialog_text=dialog_text)},
            ],
            max_tokens=1024,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        return json.loads(raw), raw
    except json.JSONDecodeError as exc:
        log.error("Failed to parse OpenAI JSON response: %s", exc)
        return None, ""
    except Exception as exc:
        log.error("OpenAI API error: %s", exc)
        return None, ""


def _messages_to_prompt(messages_text: str) -> str:
    """Convert messages_text (JSON array or legacy plain text) to a readable
    string suitable for the AI prompt."""
    try:
        msgs: list[dict] = json.loads(messages_text)
        if not isinstance(msgs, list):
            return messages_text
        lines = []
        for m in msgs:
            direction = m.get("direction", "")
            text = m.get("text", "").strip()
            time = m.get("time", "")[:16].replace("T", " ")  # "2026-04-29 21:13"
            author = m.get("author", "")
            label = "Клиент" if direction == "inbound" else (author or "Оператор")
            if text:
                lines.append(f"[{time}] {label}: {text}")
        return "\n".join(lines)
    except (json.JSONDecodeError, TypeError):
        # Legacy plain-text format — return as-is
        return messages_text


# Leading «@username» prefix that Flomni/Telegram prepends to each reply,
# used to identify the distinct authors writing in a dialog.
_LEADING_AUTHOR_RE = re.compile(r"^\s*@([A-Za-z0-9_]+)")


def _authors_in(messages_text: str) -> set[str]:
    """Distinct «@author» prefixes among inbound messages of one dialog."""
    try:
        msgs = json.loads(messages_text)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(msgs, list):
        return set()
    authors: set[str] = set()
    for m in msgs:
        if m.get("direction") != "inbound":
            continue
        match = _LEADING_AUTHOR_RE.match(m.get("text") or "")
        if match:
            authors.add(match.group(1).lower())
    return authors


# Внутренние MT-бренды в названии чата — маркер группового чата вида
# «Компания × MadeTask» (Telegram-группа заказчика). Совпадает с логикой
# extract_company_from_group_name.INTERNAL_PATTERNS.
_GROUP_MARKER_RE = re.compile(
    "|".join((
        r"madetask", r"made\s*task", r"madetak",
        r"мэйдтаск", r"мейдтаск",
        r"\bmt\b",
        r"remozo", r"ремозо",
        r"apzone", r"апзон",
        r"efficient",
        r"rosburn",
        r"\beor\b",
    )),
    re.IGNORECASE,
)


def _is_group_chat_name(name: str) -> bool:
    """True, если название чата — это групповой чат «Компания × MadeTask».

    Групповой чат = Telegram-группа заказчика с названием-парой, где явно
    присутствует внутренний MT-бренд (MadeTask / Remozo / MT / …). Диалоги
    без названия или с персональным именем групповыми НЕ считаются — их
    сторона определяется по содержанию LLM-моделью.
    """
    return bool(name) and _GROUP_MARKER_RE.search(name) is not None


def group_cabinet_ids(db) -> set[str]:
    """Return the set of client_ids that are group/corporate chats.

    A group chat is always the customer side: the payer's staff coordinate
    KYC / payout issues for several third-party executors. Group-ness is a
    property of the CABINET, so if ANY dialog of the client has a group-style
    chat_name («Компания × MadeTask»), the whole cabinet is treated as a group.

    Chats with no name or a personal name are NOT groups — their side is left
    to the LLM's content-based decision.

    Computed once per run from all dialogs, so the decision is consistent and
    can be made up-front, before the LLM call.
    """
    group: set[str] = set()
    for client_id, chat_name in db.query(Dialog.client_id, Dialog.chat_name):
        if client_id in group:
            continue
        if _is_group_chat_name(chat_name or ""):
            group.add(client_id)
    return group


def _distinct_executor_locals(messages_text: str) -> int:
    """Число РАЗНЫХ исполнителей, упомянутых в диалоге, по их email.

    Признак заказчика: одно обращение перечисляет несколько исполнителей
    (их проверки/выплаты). Считаем уникальные local-part адресов, чтобы
    вариации одного человека (rokeyaparkit74@gmail.com / @mail.com) не давали
    ложного сигнала. Внутренние адреса (support@, madetask.* и т.п.) отсеиваются.
    """
    from compute_tickets import _executor_emails  # локальный импорт: без циклов

    try:
        msgs = json.loads(messages_text)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(msgs, list):
        return 0
    locals_ = {e.split("@", 1)[0] for e in _executor_emails(msgs)}
    return len(locals_)


def _resolve_side(is_group: bool, messages_text: str) -> str:
    """Комбинированное правило стороны (детерминированное, без LLM):

    customer, если чат групповой («Компания × MadeTask») ИЛИ в диалоге
    упомянуто ≥2 разных исполнителя по email; иначе executor.
    """
    if is_group or _distinct_executor_locals(messages_text) >= 2:
        return "customer"
    return "executor"


# Cross-side category mapping: when LLM picks a thematic-correct category
# from the "wrong" side list, map to the equivalent on the target side
# instead of falling back to "Другое".
CROSS_SIDE_MAP: dict[tuple[str, str], str] = {
    # LLM returned customer-side category, target is executor:
    ("Налоговый статус исполнителя", "executor"): "Статус ИП РФ/Самозанятого",
    ("Изменения в профиле заказчика/исполнителя", "executor"): "Изменение/удаление аккаунта",
    ("Проблема KYC", "executor"): "KYC",  # legacy alias → unified KYC
    ("Дублирование KYC", "executor"): "KYC",  # свёрнута в подкатегорию KYC
    ("Запрос документов не бух", "executor"): "Запрос документов",  # legacy alias → единая категория
    ("Вопросы по числам отправки закрывашек в эдо заказчику/поторопить бухгалтерию", "executor"): "Запрос документов",
    ("Функциональность сервиса и возможности выплат", "executor"): "Функциональность сервиса и возможность выплат",  # legacy alias → единая категория
    # LLM returned executor-side category, target is customer:
    ("Проблема KYC", "customer"): "KYC",  # legacy alias → unified KYC
    ("Дублирование KYC", "customer"): "KYC",  # свёрнута в подкатегорию KYC
    ("Верификация", "customer"): "KYC",
    ("Статус ИП РФ/Самозанятого", "customer"): "Налоговый статус исполнителя",
    ("ИП РФ", "customer"): "Налоговый статус исполнителя",
    ("НДФЛ", "customer"): "Налоговый статус исполнителя",
    ("Изменение/удаление аккаунта", "customer"): "Изменения в профиле заказчика/исполнителя",
    ("Запрос документов не бух", "customer"): "Запрос документов",  # legacy alias → единая категория
    # LLM иногда склеивает две категории в одну строку — нормализуем в единую:
    ("Запрос документов / Запрос документов не бух", "customer"): "Запрос документов",
    ("Запрос документов / Запрос документов не бух", "executor"): "Запрос документов",
    # ("Потенциальный клиент", "customer") — теперь допустима для customer, маппинг убран
    ("SEPA/SWIFT", "customer"): "Выплаты и проблемы с ними",
    ("Курсы/комиссии/лимиты", "customer"): "Функциональность сервиса и возможность выплат",
    ("EOR", "customer"): "Функциональность сервиса и возможность выплат",
    ("Реквизиты заблокированы", "customer"): "Выплаты и проблемы с ними",
    ("Предложение (маркетинг, сотрудничество, банкинг)", "customer"): "Другое",
    ("Функциональность сервиса и возможности выплат", "customer"): "Функциональность сервиса и возможность выплат",
    ("Тест", "customer"): "Другое",
}


# Кириллические буквы, визуально неотличимые от латинских. Модель иногда
# присылает «КYC» с кириллической К, и точное сравнение такую категорию теряло.
_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "е": "e", "к": "k", "м": "m", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x",
})


def _fold(name: str) -> str:
    """Ключ для нестрогого сравнения: гомоглифы к латинице, регистр и края вниз."""
    return name.translate(_HOMOGLYPHS).casefold().strip()


_ALLOWED_FOLDED: dict[str, dict[str, str]] = {
    "customer": {_fold(c): c for c in CATEGORIES_CUSTOMER},
    "executor": {_fold(c): c for c in CATEGORIES_EXECUTOR},
}

_CROSS_SIDE_FOLDED: dict[tuple[str, str], str] = {
    (_fold(cat), side): target for (cat, side), target in CROSS_SIDE_MAP.items()
}


def _validate_category(side: str, category: str) -> str:
    """Ensure category is in the canonical list for the given side.

    Resolution order:
      1. If category already valid for side → return as-is.
      2. Match ignoring case and кириллические гомоглифы.
      3. Try CROSS_SIDE_MAP to translate "wrong-side" thematic categories
         to their equivalent on the target side.
      4. Fall back to 'Другое'.
    """
    allowed = CATEGORIES_CUSTOMER if side == "customer" else CATEGORIES_EXECUTOR
    if category in allowed:
        return category

    key = _fold(category)
    hit = _ALLOWED_FOLDED["customer" if side == "customer" else "executor"].get(key)
    if hit is not None:
        log.info("Normalized category %r → %r for side=%s", category, hit, side)
        return hit

    mapped = CROSS_SIDE_MAP.get((category, side)) or _CROSS_SIDE_FOLDED.get((key, side))
    if mapped and mapped in allowed:
        log.info("Cross-side mapped %r → %r for side=%s", category, mapped, side)
        return mapped

    log.warning("Category %r not in canonical list for side=%s and no cross-side map, "
                "falling back to 'Другое'", category, side)
    return "Другое"


def _validate_subcategory(category: str, subcategory: str | None) -> str | None:
    """Вернуть подкатегорию, если она валидна для данной category, иначе None.

    Категории без подкатегорий всегда дают None — модель может прислать
    непустое значение по ошибке, но мы его игнорируем.
    """
    allowed = SUBCATEGORIES.get(category)
    if not allowed:
        return None
    sub = (subcategory or "").strip()
    if sub in allowed:
        return sub
    hit = {_fold(s): s for s in allowed}.get(_fold(sub))
    if hit is not None:
        log.info("Normalized subcategory %r → %r for category %r", sub, hit, category)
        return hit
    if sub:
        log.warning("Subcategory %r not valid for category %r — ignoring.", sub, category)
    return None


def _strip_nul(value: str | None) -> str | None:
    """Postgres text columns reject NUL (0x00), which LLM responses sometimes contain."""
    if value is None:
        return None
    return value.replace("\x00", "")


NO_REQUEST_CATEGORY = "Без запроса"

# Реплики, которые клиент пишет, когда обращения по сути нет.
_TRIVIAL_INBOUND = re.compile(
    r"^(/start|start|тест|test|ok|окей|ок|да|нет|привет\w*|здравствуйте|"
    r"добрый\s+(день|вечер|утро)|доброе\s+утро|спасибо\w*|благодарю|"
    r"thanks?|thank\s+you|hi|hello|пока|до\s+свидания)[\s!.,)…]*$",
    re.I,
)


# Старые строки хранят всю переписку в text одного элемента: реплики разделены
# строкой из дефисов, направление дописано в конец реплики как « | inbound».
# Поле direction у такого элемента пустое.
_LEGACY_SPLIT = re.compile(r"\n-{5,}\s*\n")
_LEGACY_MARKER = re.compile(r"\s*\|\s*(inbound|outbound)\s*$", re.I)


def _inbound_texts(messages_text: str | None) -> list[str] | None:
    """Тексты входящих реплик. None — структуру разобрать не удалось."""
    try:
        msgs = json.loads(messages_text or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(msgs, list):
        return None

    out: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            return None
        text = m.get("text") or ""
        direction = (m.get("direction") or "").strip().lower()
        if direction:
            if direction == "inbound":
                out.append(text.strip())
            continue
        # Направления нет: либо старый склеенный формат, либо мусор.
        for block in _LEGACY_SPLIT.split(text):
            marker = _LEGACY_MARKER.search(block)
            if marker is None:
                continue
            if marker.group(1).lower() == "inbound":
                out.append(block[: marker.start()].strip())
    return out


def _has_substantive_inbound(messages_text: str | None) -> bool:
    """Написал ли клиент хоть что-то по существу.

    Пустое касание — это диалог, где входящих сообщений нет вовсе (клиенту
    ответил только бот или оператор) либо все они тривиальные: «/start»,
    «привет», «спасибо». Обращения не было, разбирать нечего.

    Картинки и файлы приходят с ссылкой в тексте, поэтому по тексту их видно.
    """
    texts = _inbound_texts(messages_text)
    if texts is None:
        return True  # не разобрали структуру — считаем содержательным
    for text in texts:
        if len(text) > 3 and not _TRIVIAL_INBOUND.match(text):
            return True
    return False


def run() -> None:
    log.info("Starting AI analysis job.")

    with get_session() as db:
        pending: list[Dialog] = (
            db.query(Dialog).filter(Dialog.processed.is_(False)).all()
        )
        group_clients = group_cabinet_ids(db)

    log.info("Found %d unprocessed dialogs (%d group cabinets).",
             len(pending), len(group_clients))

    for dialog in pending:
        if not dialog.messages_text or not dialog.messages_text.strip():
            log.debug("Dialog %d has no text, skipping.", dialog.id)
            continue

        # Сторона определяется детерминированно (не LLM): заказчик, если чат
        # групповой ИЛИ в диалоге упомянуто ≥2 разных исполнителя по email
        # (персонал плательщика координирует проверки/выплаты). Иначе —
        # исполнитель. LLM оставляем только для категории/summary.
        is_group = dialog.client_id in group_clients

        log.info(
            "Analysing dialog %d (source=%s, client=%s, group=%s).",
            dialog.id, dialog.source, dialog.client_id, is_group,
        )

        result, raw_response = _call_openai(_messages_to_prompt(dialog.messages_text))

        if result is None:
            log.warning("No result for dialog %d, will retry next run.", dialog.id)
            continue

        side = _resolve_side(is_group, dialog.messages_text)
        category = _validate_category(side, (result.get("category") or "").strip())
        if not _has_substantive_inbound(dialog.messages_text):
            if category != NO_REQUEST_CATEGORY:
                log.info("Dialog %d: нет содержательного inbound, %r → %r",
                         dialog.id, category, NO_REQUEST_CATEGORY)
            category = NO_REQUEST_CATEGORY
        subcategory = _validate_subcategory(category, result.get("subcategory"))

        with get_session() as db:
            analysis = AnalysisResult(
                dialog_id=dialog.id,
                summary=_strip_nul(result.get("summary", "")),
                category=category,
                subcategory=subcategory,
                side=side,
                sentiment=_strip_nul(result.get("sentiment", "")),
                priority=_strip_nul(result.get("priority", "")),
                resolution=_strip_nul(result.get("resolution", "")),
                rationale=_strip_nul(result.get("rationale", "")),
                raw_response=_strip_nul(raw_response),
            )
            db.add(analysis)

            db_dialog = db.get(Dialog, dialog.id)
            if db_dialog:
                db_dialog.processed = True
                db_dialog.updated_at = datetime.now(timezone.utc)

            db.commit()

        log.info(
            "Dialog %d → side=%s | category=%s | subcategory=%s | sentiment=%s | priority=%s | resolution=%s",
            dialog.id, side, category, subcategory,
            result.get("sentiment"), result.get("priority"), result.get("resolution"),
        )

    log.info("AI analysis job complete.")


if __name__ == "__main__":
    run()
