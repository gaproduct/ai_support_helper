"""
KB training module.

Flow:
  1. User pastes article/FAQ text.
  2. OpenAI splits it into Q&A chunks with category/subcategory/audience.
  3. User reviews and edits chunks in the UI.
  4. Each chunk is embedded and uploaded to Supabase kb_articles.
  5. For each article, OpenAI generates question variations → question_variations.
"""

import json
import logging

import httpx
from openai import OpenAI

from core.config import settings

log = logging.getLogger(__name__)

openai_client = OpenAI(api_key=settings.openai_api_key)

CHUNK_SYSTEM_PROMPT = """Ты помощник по созданию базы знаний для службы поддержки платформы MadeTask.
Разбей предоставленный текст на отдельные Q&A-пары.

Для каждой пары верни:
- question   — вопрос как его задал бы клиент поддержки
- answer     — чёткий и конкретный ответ
- category   — тема/раздел (например: Выплаты, Аккаунт, Задания, Техническое, Маркировка)
- subcategory — уточнённая подтема (можно пустую строку)
- audience   — кому адресована статья: "исполнитель", "заказчик" или "оба"

Правила:
- Каждая пара самодостаточна без внешнего контекста
- Не придумывай информацию которой нет в тексте
- audience строго одно из трёх значений: исполнитель / заказчик / оба

Верни ТОЛЬКО валидный JSON-массив:
[
  {
    "question": "...",
    "answer": "...",
    "category": "...",
    "subcategory": "...",
    "audience": "оба"
  }
]"""

VARIATIONS_PROMPT = """Сгенерируй 4 разных способа задать тот же вопрос другими словами.
Вопрос: {question}

Верни ТОЛЬКО JSON-массив строк:
["вариация 1", "вариация 2", "вариация 3", "вариация 4"]"""


def chunk_text(text: str) -> list[dict]:
    """Send article text to OpenAI → list of chunk dicts."""
    try:
        response = openai_client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=2048,
            temperature=0,
        )
        raw = response.choices[0].message.content or "[]"
        chunks = json.loads(raw)
        if not isinstance(chunks, list):
            return []
        return [c for c in chunks if c.get("question") and c.get("answer")]
    except json.JSONDecodeError as exc:
        log.error("Failed to parse chunks JSON: %s", exc)
        return []
    except Exception as exc:
        log.error("OpenAI chunk error: %s", exc)
        return []


def _get_embedding(text: str) -> list[float] | None:
    try:
        resp = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=text.replace("\n", " "),
        )
        return resp.data[0].embedding
    except Exception as exc:
        log.error("Embedding error: %s", exc)
        return None


def _generate_variations(question: str) -> list[str]:
    """Generate alternative phrasings for a question."""
    try:
        response = openai_client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "user", "content": VARIATIONS_PROMPT.format(question=question)},
            ],
            max_tokens=512,
            temperature=0.7,
        )
        raw = response.choices[0].message.content or "[]"
        variations = json.loads(raw)
        return variations if isinstance(variations, list) else []
    except Exception as exc:
        log.error("Variations error: %s", exc)
        return []


def _upload_variations(article_id: str, question: str, headers: dict) -> None:
    """Generate question variations and upload them to question_variations."""
    variations = _generate_variations(question)
    url = f"{settings.supabase_url}/rest/v1/question_variations"

    for variation in variations:
        if not variation.strip():
            continue
        embedding = _get_embedding(variation)
        if embedding is None:
            continue
        try:
            httpx.post(
                url,
                headers=headers,
                json={"article_id": article_id, "variation": variation, "embedding": embedding},
                timeout=15,
            )
        except Exception as exc:
            log.error("Failed to upload variation: %s", exc)


def upload_chunks(chunks: list[dict]) -> tuple[int, list[str]]:
    """
    Embed each chunk and insert into Supabase kb_articles + question_variations.
    Returns (uploaded_count, errors).
    """
    url = f"{settings.supabase_url}/rest/v1/kb_articles"
    headers = {
        "apikey": settings.supabase_key,
        "Authorization": f"Bearer {settings.supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    uploaded = 0
    errors: list[str] = []

    for i, chunk in enumerate(chunks):
        question = chunk.get("question", "").strip()
        answer = chunk.get("answer", "").strip()
        category = chunk.get("category", "").strip()
        audience = chunk.get("audience", "оба").strip()

        if not question or not answer:
            errors.append(f"Chunk {i+1}: пустой вопрос или ответ")
            continue
        if not category:
            errors.append(f"Chunk {i+1}: не указана категория")
            continue
        if audience not in ("исполнитель", "заказчик", "оба"):
            audience = "оба"

        embedding = _get_embedding(f"{question}\n{answer}")
        if embedding is None:
            errors.append(f"Chunk {i+1}: не удалось получить эмбеддинг")
            continue

        row = {
            "question": question,
            "answer": answer,
            "category": category,
            "subcategory": chunk.get("subcategory", "") or None,
            "audience": audience,
            "embedding": embedding,
        }

        try:
            resp = httpx.post(url, headers=headers, json=row, timeout=15)
            if resp.status_code in (200, 201):
                uploaded += 1
                log.info("Uploaded chunk %d: %r", i + 1, question[:60])
                # Upload question variations
                article_data = resp.json()
                if article_data and isinstance(article_data, list):
                    article_id = article_data[0].get("id")
                    if article_id:
                        _upload_variations(article_id, question, {
                            "apikey": settings.supabase_key,
                            "Authorization": f"Bearer {settings.supabase_key}",
                            "Content-Type": "application/json",
                            "Prefer": "return=minimal",
                        })
            else:
                errors.append(f"Chunk {i+1}: Supabase вернул {resp.status_code} — {resp.text[:120]}")
        except Exception as exc:
            errors.append(f"Chunk {i+1}: {exc}")

    return uploaded, errors
