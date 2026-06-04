"""
Greeting detection + KB search via OpenAI embeddings + Supabase pgvector RPC.

Scenario detection (verification / balance top-up / ...) lives in the
`scenarios` package and is composed by callers (ai_response, chat_session,
api/search). This module is intentionally limited to: 'is the message a
greeting?' and 'find the closest KB article'.
"""

import logging
import re

import httpx
from openai import OpenAI

from config import settings

log = logging.getLogger(__name__)

openai_client = OpenAI(api_key=settings.openai_api_key)

EMBEDDING_MODEL = "text-embedding-3-small"
MATCH_THRESHOLD = 0.4
MATCH_COUNT = 1

_GREETING_PATTERN = re.compile(
    r"^("
    r"привет|privet|hi|hello|hey|"
    r"здравствуй(те)?|здраст(вуй(те)?)?|"
    r"добрый\s+(день|вечер|утро)|"
    r"доброе\s+утро|доброго\s+(дня|времени\s+суток)|"
    r"добрый|хай|хэй|"
    r"good\s+(morning|afternoon|evening|day)"
    r")[!.,\s]*$",
    re.IGNORECASE,
)


def _is_greeting(text: str) -> bool:
    """True when the message is a pure greeting with no question substance."""
    return bool(_GREETING_PATTERN.match(text.strip()))


def _get_embedding(text: str) -> list[float] | None:
    try:
        resp = openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text.replace("\n", " "),
        )
        return resp.data[0].embedding
    except Exception as exc:
        log.error("OpenAI embedding error: %s", exc)
        return None


def _search_kb(embedding: list[float], threshold: float) -> dict | None:
    url = f"{settings.supabase_url}/rest/v1/rpc/match_kb_with_variations"
    headers = {
        "apikey": settings.supabase_key,
        "Authorization": f"Bearer {settings.supabase_key}",
        "Content-Type": "application/json",
    }
    body = {
        "query_embedding": embedding,
        "match_threshold": threshold,
        "match_count": MATCH_COUNT,
    }
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        results: list[dict] = resp.json()
        return results[0] if results else None
    except Exception as exc:
        log.error("Supabase KB search error: %s", exc)
        return None


def search(message_text: str, threshold: float = MATCH_THRESHOLD) -> dict:
    """
    Return {'article': dict|None, 'is_greeting': bool}.
    Empty / greeting messages skip the KB search entirely.
    """
    result: dict = {"article": None, "is_greeting": False}

    if not message_text or not message_text.strip():
        return result

    if _is_greeting(message_text):
        result["is_greeting"] = True
        return result

    if not settings.supabase_url or not settings.supabase_key:
        return result

    embedding = _get_embedding(message_text)
    if embedding:
        result["article"] = _search_kb(embedding, threshold)
    return result
