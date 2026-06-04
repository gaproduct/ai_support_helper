"""
AI analysis worker — runs every 24 hours.

Logic (mirrors Make "Support Ticket AI Research" scenario):
  1. Fetch all Dialogs where processed=False.
  2. For each dialog, send the accumulated messages_text to OpenAI and request
     a structured JSON response with summary, category, subcategory,
     sentiment, priority, and resolution.
  3. Store the result in AnalysisResult and mark dialog.processed = True.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Union

from openai import OpenAI

from config import settings
from database import AnalysisResult, Dialog, get_session


log = logging.getLogger(__name__)

client = OpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """Ты аналитик службы поддержки клиентов.
Проанализируй переписку и верни ТОЛЬКО валидный JSON-объект со следующими полями:

  "summary"     — краткое содержание диалога (1-3 предложения на языке оригинала),
  "category"    — основная категория обращения (например: Billing, Technical, Account, Shipping, Other),
  "subcategory" — уточнённая подкатегория внутри категории,
  "sentiment"   — тональность клиента: "positive", "neutral" или "negative",
  "priority"    — приоритет тикета: "low", "medium", "high" или "critical",
  "resolution"  — итог диалога: "resolved", "unresolved" или "escalated".

Не добавляй никакого текста вне JSON-объекта."""

USER_PROMPT_TEMPLATE = """Проанализируй следующую переписку поддержки:

{dialog_text}"""


def _call_openai(dialog_text: str) -> tuple[dict | None, str]:
    """
    Send dialog text to OpenAI and return (parsed_dict, raw_json_string).
    Returns (None, "") on error.
    """
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(dialog_text=dialog_text)},
            ],
            max_tokens=1024,
            temperature=0,
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
    """
    Convert messages_text (JSON array or legacy plain text) to a readable
    string suitable for the AI prompt.
    """
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

        with get_session() as db:
            analysis = AnalysisResult(
                dialog_id=dialog.id,
                summary=result.get("summary", ""),
                category=result.get("category", ""),
                subcategory=result.get("subcategory", ""),
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
            "Dialog %d → category=%s | subcategory=%s | sentiment=%s | priority=%s | resolution=%s",
            dialog.id,
            result.get("category"),
            result.get("subcategory"),
            result.get("sentiment"),
            result.get("priority"),
            result.get("resolution"),
        )

    log.info("AI analysis job complete.")


if __name__ == "__main__":
    run()
