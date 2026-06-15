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
from datetime import datetime, timezone

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
    "Проблема KYC",
    "Дублирование KYC",
    "Выплаты и проблемы с ними",
    "Техническая проблема/вопрос",
    "Запрос документов не бух",
    "Зачисление платежа на баланс",
    "Вопросы по числам отправки закрывашек в эдо заказчику/поторопить бухгалтерию",
    "Вопросы по работе в сервисе",
    "Функциональность сервиса и возможность выплат",
    "Изменения в профиле заказчика/исполнителя",
    "Налоговый статус исполнителя",
    "Другое",
)

CATEGORIES_EXECUTOR: tuple[str, ...] = (
    "Другое",
    "Выплаты и проблемы с ними",
    "KYC",
    "ИП РФ",
    "SEPA/SWIFT",
    "Изменение/удаление аккаунта",
    "Курсы/комиссии/лимиты",
    "Функциональность сервиса и возможности выплат",
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


def _format_list(items: tuple[str, ...]) -> str:
    return "\n".join(f"  - {x}" for x in items)


SYSTEM_PROMPT = f"""Ты аналитик службы поддержки B2B-платёжного сервиса.
Проанализируй переписку и верни ТОЛЬКО валидный JSON-объект (без markdown,
без комментариев) со следующими полями:

  "side"        — сторона клиента в обращении. ОДНО ИЗ:
                  "customer"  — действующий заказчик (компания/ИП, которые
                                уже работают с сервисом и оплачивают задачи)
                  "executor"  — исполнитель (фрилансер/контрактор, получающий
                                выплаты), а также НЕ-зарегистрированные
                                пользователи и потенциальные новые клиенты
                                (исторически заходят со стороны executor)
  "category"    — основная категория обращения. Должна быть ровно одной из
                  списков ниже, в зависимости от side:

  Если side="customer":
{_format_list(CATEGORIES_CUSTOMER)}

  Если side="executor":
{_format_list(CATEGORIES_EXECUTOR)}

  Особое правило: если клиент пишет, но ещё НЕ работает с сервисом и
  интересуется условиями (комиссия, ставки, объёмы, демо, договор для
  юр.лица, «как начать сотрудничество», стартовые условия, передача
  персональному менеджеру) — это side="executor", category="{POTENTIAL_CUSTOMER_CATEGORY}".

  "summary"     — краткое содержание диалога (1-3 предложения на языке оригинала)
  "sentiment"   — тональность клиента: "positive", "neutral" или "negative"
  "priority"    — приоритет тикета: "low", "medium", "high" или "critical"
  "resolution"  — итог диалога: "resolved", "unresolved" или "escalated"

Не добавляй никакого текста вне JSON-объекта."""


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


def _validate_category(side: str, category: str) -> str:
    """Ensure category is in the canonical list for the given side. If not,
    fall back to 'Другое' so the row stays insertable."""
    allowed = CATEGORIES_CUSTOMER if side == "customer" else CATEGORIES_EXECUTOR
    if category in allowed:
        return category
    log.warning("Category %r not in canonical list for side=%s, falling back to 'Другое'",
                category, side)
    return "Другое"


def run() -> None:
    log.info("Starting AI analysis job.")

    with get_session() as db:
        pending: list[Dialog] = (
            db.query(Dialog).filter(Dialog.processed.is_(False)).all()
        )

    log.info("Found %d unprocessed dialogs.", len(pending))

    for dialog in pending:
        if not dialog.messages_text or not dialog.messages_text.strip():
            log.debug("Dialog %d has no text, skipping.", dialog.id)
            continue

        log.info(
            "Analysing dialog %d (source=%s, client=%s).",
            dialog.id, dialog.source, dialog.client_id,
        )

        result, raw_response = _call_openai(_messages_to_prompt(dialog.messages_text))

        if result is None:
            log.warning("No result for dialog %d, will retry next run.", dialog.id)
            continue

        side = (result.get("side") or "").strip().lower()
        if side not in ("customer", "executor"):
            log.warning("Dialog %d: invalid side=%r, defaulting to 'executor'.",
                        dialog.id, side)
            side = "executor"
        category = _validate_category(side, (result.get("category") or "").strip())

        with get_session() as db:
            analysis = AnalysisResult(
                dialog_id=dialog.id,
                summary=result.get("summary", ""),
                category=category,
                side=side,
                sentiment=result.get("sentiment", ""),
                priority=result.get("priority", ""),
                resolution=result.get("resolution", ""),
                raw_response=raw_response,
            )
            db.add(analysis)

            db_dialog = db.get(Dialog, dialog.id)
            if db_dialog:
                db_dialog.processed = True
                db_dialog.updated_at = datetime.now(timezone.utc)

            db.commit()

        log.info(
            "Dialog %d → side=%s | category=%s | sentiment=%s | priority=%s | resolution=%s",
            dialog.id, side, category,
            result.get("sentiment"), result.get("priority"), result.get("resolution"),
        )

    log.info("AI analysis job complete.")


if __name__ == "__main__":
    run()
