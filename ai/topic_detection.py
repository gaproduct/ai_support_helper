"""
Hybrid new-topic detection for incoming Flomni messages.

Question: when a new inbound message arrives for a client with an open
IncomingMessage, is it a continuation of the same topic or a brand-new
question (operator should treat it as a new ticket)?

Hybrid rules (Option D):
  1. Time gap since last message >= HARD_GAP_HOURS → new topic.
  2. Time gap >= SOFT_GAP_HOURS AND new message starts with a greeting →
     new topic (client is opening a fresh dialog after a pause).
  3. Time gap < QUICK_GAP_MINUTES AND no greeting → continuation.
  4. Otherwise (ambiguous middle ground) → ask the LLM classifier.

The LLM is only invoked in case 4 to keep latency and cost low.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from openai import OpenAI

from ai.auto_response import _GREETING_PATTERN
from core.config import settings

log = logging.getLogger(__name__)

HARD_GAP_HOURS = 24
SOFT_GAP_HOURS = 2
QUICK_GAP_MINUTES = 30

_openai_client = OpenAI(api_key=settings.openai_api_key)
_CLASSIFIER_MODEL = settings.openai_model or "gpt-4.1-nano"

# Greeting prefix: greeting word at the start, with optional trailing chars/text.
_GREETING_PREFIX = re.compile(
    r"^(привет|privet|hi|hello|hey|здравствуй(те)?|здраст(вуй(те)?)?|"
    r"добрый\s+(день|вечер|утро)|доброе\s+утро|доброго\s+(дня|времени\s+суток)|"
    r"добрый|хай|хэй|good\s+(morning|afternoon|evening|day))\b",
    re.IGNORECASE,
)


def _starts_with_greeting(text: str) -> bool:
    return bool(_GREETING_PREFIX.match(text.strip()))


def _parse_iso(ts: str) -> datetime | None:
    """Parse Flomni ISO timestamp like '2026-04-29T21:13:11.888Z'."""
    if not ts:
        return None
    try:
        # Python's fromisoformat doesn't accept the trailing 'Z' before 3.11
        normalised = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        log.warning("Could not parse timestamp: %s", ts)
        return None


def _gap_hours(last_message_at: str, now: datetime | None = None) -> float | None:
    last = _parse_iso(last_message_at)
    if last is None:
        return None
    current = now or datetime.now(timezone.utc)
    return (current - last).total_seconds() / 3600.0


def _llm_is_new_topic(previous_text: str, new_message: str) -> bool:
    """
    Ask the LLM whether the new message starts a new topic.
    Falls back to False (continuation) on any error to avoid splitting threads
    incorrectly on infra hiccups.
    """
    prompt = (
        "Ты классификатор сообщений в саппорт-чате. Тебе дан накопленный "
        "контекст предыдущего диалога с клиентом и новое входящее сообщение.\n"
        "Определи, является ли новое сообщение продолжением того же вопроса "
        "(same_topic) или начинает новую тему/вопрос (new_topic).\n"
        "Ответь одним словом: same_topic или new_topic.\n\n"
        f"Контекст диалога:\n{previous_text[-2000:]}\n\n"
        f"Новое сообщение клиента:\n{new_message}"
    )
    try:
        resp = _openai_client.chat.completions.create(
            model=_CLASSIFIER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=5,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        log.info("LLM topic classifier verdict: %s", answer)
        return "new_topic" in answer
    except Exception as exc:
        log.error("LLM topic classifier failed: %s", exc)
        return False


def is_new_topic(
    previous_text: str,
    last_message_at: str,
    new_message: str,
    now: datetime | None = None,
) -> bool:
    """
    Decide whether `new_message` opens a new topic relative to the accumulated
    `previous_text` (last message timestamp `last_message_at`).
    """
    new_message = (new_message or "").strip()
    if not new_message:
        return False

    gap = _gap_hours(last_message_at, now=now)
    greeting = _starts_with_greeting(new_message)

    # Rule 1: hard time gap → always new topic
    if gap is not None and gap >= HARD_GAP_HOURS:
        log.info("Topic boundary: gap=%.1fh >= HARD_GAP", gap)
        return True

    # Rule 2: medium gap + greeting prefix → new topic
    if gap is not None and gap >= SOFT_GAP_HOURS and greeting:
        log.info("Topic boundary: gap=%.1fh + greeting", gap)
        return True

    # Rule 3: very recent + no greeting → continuation
    if gap is not None and gap * 60 < QUICK_GAP_MINUTES and not greeting:
        return False

    # Rule 4: ambiguous → LLM
    if not (previous_text or "").strip():
        return False
    return _llm_is_new_topic(previous_text, new_message)
