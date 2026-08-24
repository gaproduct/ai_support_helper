"""Индекс диалогов для кабинета оператора.

Наполняет две таблицы из миграции 010:

  dialog_embeddings — вектор по формулировке проблемы клиента
  dialog_outcomes   — «чем закончилось», короткая выжимка по ответам поддержки

Почему вектор считаем только по входящим сообщениям клиента. В момент поиска у
нас на руках лишь текст нового обращения. Если бы в индекс попадали ответы
поддержки и AI-пересказ всего диалога, мы бы сравнивали вопрос с ответом и
похожесть поехала бы. Поэтому индексируем ровно то, с чем будем сравнивать:
первые содержательные реплики клиента.

Служебные заглушки и опросы качества отсекает resolution_metrics.normalize, тот
же код, что считает метрики решения. Теневые дубли Flomni в индекс не берём,
иначе один кейс покажется оператору дважды.

  python dialog_index.py --limit 50        # пробный прогон
  python dialog_index.py                   # досчитать всё, чего не хватает
"""
import argparse
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
from sqlalchemy import text

import resolution_metrics as rm
from config import settings
from database import get_session

log = logging.getLogger(__name__)

openai_client = OpenAI(api_key=settings.openai_api_key)

EMBEDDING_MODEL = "text-embedding-3-small"
# Укороченный вектор. Качество на нашей задаче не отличается от полного, а места
# и времени на косинус уходит втрое меньше.
EMBEDDING_DIMS = 512
EMBEDDING_BATCH = 100

OUTCOME_MODEL = settings.openai_model
# Сколько диалогов выжимаем параллельно. Больше упирается в лимиты OpenAI.
OUTCOME_WORKERS = 8

# Читаем пачками, чтобы не тянуть в память весь messages_json разом.
DB_BATCH = 200

# Границы текста, который уходит в модель.
MAX_CLIENT_CHARS = 1500
MAX_DIALOG_CHARS = 6000
MAX_TAIL_MESSAGES = 30

OUTCOME_PROMPT = (
    "Ты помогаешь оператору поддержки быстро понять, чем закончилось похожее "
    "обращение. По переписке напиши, что поддержка сделала и с каким результатом. "
    "От одного до трёх предложений, простым языком, без вводных оборотов. "
    "Не пересказывай вопрос клиента, он оператору уже виден. "
    "Если по переписке не видно, что вопрос решили, так и напиши."
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _messages(payload: str | None) -> list[rm.Msg]:
    try:
        raw = json.loads(payload or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return rm.normalize(raw)


def client_statement(msgs: list[rm.Msg]) -> str:
    """Формулировка проблемы словами клиента. Это и есть тело индекса."""
    parts: list[str] = []
    total = 0
    for m in msgs:
        if not (m.is_client and m.substantive):
            continue
        body = m.text.strip()
        parts.append(body)
        total += len(body)
        if total >= MAX_CLIENT_CHARS:
            break
    return "\n".join(parts)[:MAX_CLIENT_CHARS]


def dialog_transcript(msgs: list[rm.Msg]) -> str:
    """Хвост переписки для выжимки: кто что сказал, без заглушек."""
    lines = [
        ("Клиент: " if m.is_client else "Поддержка: ") + m.text.strip()
        for m in msgs
        if m.substantive
    ]
    return "\n".join(lines[-MAX_TAIL_MESSAGES:])[-MAX_DIALOG_CHARS:]


def _load_existing(table: str) -> dict[int, str]:
    with get_session() as db:
        rows = db.execute(text(f"SELECT dialog_id, source_hash FROM {table}")).fetchall()
    return {r[0]: r[1] for r in rows}


def _iter_dialogs(limit: int | None):
    """Диалоги с AI-разбором, пачками, без теневых дублей."""
    last_id = 0
    seen = 0
    while True:
        with get_session() as db:
            rows = db.execute(text("""
                SELECT d.id, d.messages_json
                FROM dialogs d
                JOIN analysis_results a ON a.dialog_id = d.id
                WHERE d.id > :last
                  AND d.messages_json IS NOT NULL
                  AND d.messages_json NOT IN ('', '[]')
                  AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                                  WHERE s.dialog_id = d.id)
                ORDER BY d.id
                LIMIT :batch
            """), {"last": last_id, "batch": DB_BATCH}).fetchall()
        if not rows:
            return
        for dialog_id, payload in rows:
            yield dialog_id, payload
            seen += 1
            if limit is not None and seen >= limit:
                return
        last_id = rows[-1][0]


def _embed(texts: list[str]) -> list[list[float]]:
    resp = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[t.replace("\n", " ") for t in texts],
        dimensions=EMBEDDING_DIMS,
    )
    return [item.embedding for item in resp.data]


def _summarize(transcript: str) -> str | None:
    try:
        resp = openai_client.chat.completions.create(
            model=OUTCOME_MODEL,
            messages=[
                {"role": "system", "content": OUTCOME_PROMPT},
                {"role": "user", "content": transcript},
            ],
            temperature=0,
            max_tokens=180,
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as exc:
        log.error("outcome failed: %s", exc)
        return None


def _save_embeddings(batch: list[tuple[int, str, list[float]]]) -> None:
    with get_session() as db:
        for dialog_id, source_hash, vec in batch:
            db.execute(text("""
                INSERT INTO dialog_embeddings (dialog_id, vec, model, source_hash, computed_at)
                VALUES (:id, :vec, :model, :hash, now())
                ON CONFLICT (dialog_id) DO UPDATE
                SET vec = EXCLUDED.vec, model = EXCLUDED.model,
                    source_hash = EXCLUDED.source_hash, computed_at = now()
            """), {"id": dialog_id, "vec": vec, "model": EMBEDDING_MODEL, "hash": source_hash})
        db.commit()


def _save_outcomes(batch: list[tuple[int, str, str | None]]) -> None:
    with get_session() as db:
        for dialog_id, source_hash, outcome in batch:
            db.execute(text("""
                INSERT INTO dialog_outcomes (dialog_id, outcome, model, source_hash, computed_at)
                VALUES (:id, :outcome, :model, :hash, now())
                ON CONFLICT (dialog_id) DO UPDATE
                SET outcome = EXCLUDED.outcome, model = EXCLUDED.model,
                    source_hash = EXCLUDED.source_hash, computed_at = now()
            """), {"id": dialog_id, "outcome": outcome, "model": OUTCOME_MODEL, "hash": source_hash})
        db.commit()


def run(limit: int | None = None, skip_outcomes: bool = False) -> dict[str, int]:
    have_vec = _load_existing("dialog_embeddings")
    have_out = _load_existing("dialog_outcomes")

    pending_vec: list[tuple[int, str, str]] = []   # (id, hash, text)
    pending_out: list[tuple[int, str, str]] = []
    stats = {"seen": 0, "embedded": 0, "summarized": 0}

    def flush_vec() -> None:
        if not pending_vec:
            return
        vectors = _embed([t for _, _, t in pending_vec])
        _save_embeddings([
            (dialog_id, source_hash, vec)
            for (dialog_id, source_hash, _), vec in zip(pending_vec, vectors)
        ])
        stats["embedded"] += len(pending_vec)
        pending_vec.clear()

    def flush_out() -> None:
        if not pending_out:
            return
        with ThreadPoolExecutor(max_workers=OUTCOME_WORKERS) as pool:
            results = list(pool.map(_summarize, [t for _, _, t in pending_out]))
        _save_outcomes([
            (dialog_id, source_hash, outcome)
            for (dialog_id, source_hash, _), outcome in zip(pending_out, results)
        ])
        stats["summarized"] += len(pending_out)
        pending_out.clear()

    for dialog_id, payload in _iter_dialogs(limit):
        stats["seen"] += 1
        msgs = _messages(payload)
        if not msgs:
            continue

        statement = client_statement(msgs)
        if statement:
            source_hash = _hash(statement)
            if have_vec.get(dialog_id) != source_hash:
                pending_vec.append((dialog_id, source_hash, statement))

        if not skip_outcomes:
            transcript = dialog_transcript(msgs)
            if transcript:
                source_hash = _hash(transcript)
                if have_out.get(dialog_id) != source_hash:
                    pending_out.append((dialog_id, source_hash, transcript))

        if len(pending_vec) >= EMBEDDING_BATCH:
            flush_vec()
        if len(pending_out) >= OUTCOME_WORKERS * 4:
            flush_out()
            log.info("indexed %(seen)s dialogs, outcomes %(summarized)s", stats)

    flush_vec()
    flush_out()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx пишет строку на каждый запрос к OpenAI, за ними не видно прогресса.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="обработать не больше N диалогов")
    ap.add_argument("--skip-outcomes", action="store_true",
                    help="только векторы, без обращений к чат-модели")
    args = ap.parse_args()

    stats = run(limit=args.limit, skip_outcomes=args.skip_outcomes)
    print(f"просмотрено: {stats['seen']}   векторов: {stats['embedded']}   "
          f"выжимок: {stats['summarized']}")


if __name__ == "__main__":
    main()
