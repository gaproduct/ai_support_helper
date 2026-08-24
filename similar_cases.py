"""Поиск похожих обращений для кабинета оператора.

Два шага. Сначала отбираем кандидатов по категории из AI-разбора, потом внутри
отбора ранжируем по близости текста. Категория отсекает чужие темы, вектор
находит именно ту формулировку, с которой пришёл клиент.

Если по категории кандидатов почти нет, отбор расширяется на все категории.
Лучше показать пять близких по смыслу, чем ничего.

Косинус считается в питоне. После отбора кандидатов остаются сотни, и полный
перебор занимает десятки миллисекунд. Отдельный векторный индекс тут не нужен.
"""
import json
import logging
from operator import mul

from sqlalchemy import text

import resolution_metrics as rm
from dialog_index import EMBEDDING_DIMS, EMBEDDING_MODEL, openai_client

log = logging.getLogger(__name__)

# Сколько кандидатов тянем из базы до ранжирования.
MAX_CANDIDATES = 2500
# Ниже этого порога считаем, что похожего нет, и лучше промолчать.
MIN_SCORE = 0.35
# Если по категории набралось меньше, ищем по всем категориям.
MIN_CANDIDATES = 20
# Сколько ответов поддержки показываем в раскрывающемся блоке.
MAX_SUPPORT_REPLIES = 3


def _norm(vec: list[float]) -> list[float]:
    length = sum(v * v for v in vec) ** 0.5
    return [v / length for v in vec] if length else vec


def embed_query(query: str) -> list[float] | None:
    try:
        resp = openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=query.replace("\n", " "),
            dimensions=EMBEDDING_DIMS,
        )
        return _norm(resp.data[0].embedding)
    except Exception as exc:
        log.error("similar_cases: embedding failed: %s", exc)
        return None


def _candidates(db, category: str | None, side: str | None, exclude_id: int | None):
    """Диалоги с вектором, свежие сверху. Теневые дубли уже отсеяны индексом."""
    sql = """
        SELECT e.dialog_id, e.vec, a.summary, a.category, a.subcategory,
               a.resolution, d.source, d.client_id,
               COALESCE(d.dialog_date, d.created_at::date) AS dt,
               o.outcome
        FROM dialog_embeddings e
        JOIN dialogs d          ON d.id = e.dialog_id
        JOIN analysis_results a ON a.dialog_id = e.dialog_id
        LEFT JOIN dialog_outcomes o ON o.dialog_id = e.dialog_id
        WHERE true
    """
    params: dict = {}
    if exclude_id is not None:
        sql += " AND e.dialog_id <> :exclude"
        params["exclude"] = exclude_id
    if category:
        sql += " AND a.category = :category"
        params["category"] = category
    if side:
        sql += " AND a.side = :side"
        params["side"] = side
    sql += " ORDER BY dt DESC NULLS LAST LIMIT :cap"
    params["cap"] = MAX_CANDIDATES
    return db.execute(text(sql), params).fetchall()


def _support_replies(db, dialog_id: int) -> list[str]:
    """Последние содержательные ответы поддержки в том диалоге."""
    row = db.execute(
        text("SELECT messages_json FROM dialogs WHERE id = :id"), {"id": dialog_id}
    ).first()
    if not row or not row[0]:
        return []
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    replies = [
        m.text.strip()
        for m in rm.normalize(raw)
        if m.is_support and m.substantive
    ]
    return replies[-MAX_SUPPORT_REPLIES:]


def find(
    db,
    query: str,
    category: str | None = None,
    side: str | None = None,
    exclude_dialog_id: int | None = None,
    limit: int = 5,
) -> list[dict]:
    """Похожие обращения с решением, от самого близкого."""
    if not query or not query.strip():
        return []

    vector = embed_query(query)
    if vector is None:
        return []

    rows = _candidates(db, category, side, exclude_dialog_id)
    if len(rows) < MIN_CANDIDATES and category:
        # Категория оказалась слишком узкой или проставлена неудачно.
        rows = _candidates(db, None, side, exclude_dialog_id)

    # У диалога может быть несколько разборов, тогда он приходит из базы дважды.
    # Берём каждый диалог один раз, иначе оператор увидит один кейс два раза.
    scored: dict[int, tuple[float, object]] = {}
    for row in rows:
        if row.dialog_id in scored:
            continue
        vec = _norm(list(row.vec))
        score = sum(map(mul, vector, vec))
        if score >= MIN_SCORE:
            scored[row.dialog_id] = (score, row)
    ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)

    results = []
    for score, row in ranked[:limit]:
        results.append({
            "dialog_id": row.dialog_id,
            "score": round(score, 3),
            "summary": row.summary,
            "category": row.category,
            "subcategory": row.subcategory,
            "resolution": row.resolution,
            "outcome": row.outcome,
            "source": row.source,
            "client_id": row.client_id,
            "date": row.dt.isoformat() if row.dt else None,
            "support_replies": _support_replies(db, row.dialog_id),
        })
    return results
