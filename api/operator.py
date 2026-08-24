"""Карточка обращения для кабинета оператора.

Оператор жмёт в Slack-треде кнопку «Карточка» и попадает сюда с id обращения.
Собираем в один ответ четыре блока:

  ticket    что за обращение и от кого
  similar   пять похожих кейсов с решением
  executor  задачи, выплаты и заказчики исполнителя из Superset
  history   с чем этот же клиент писал в поддержку раньше

Блоки независимы. Если Superset лежит или исполнитель не опознан, остальное всё
равно покажется: оператору лучше половина карточки, чем страница с ошибкой.

Категорию для похожих кейсов считаем на лету. Ночная джоба ai_analysis до
свежего обращения ещё не дошла, а без категории отбор ловит чужие темы.
Сторону (заказчик или исполнитель) берём из прошлых разборов этого же чата,
она у клиента не меняется.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

import ai_analysis
import operator_profile
import similar_cases
from config import settings
from database import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/operator", tags=["operator"])

SIMILAR_LIMIT = 5
HISTORY_LIMIT = 10
# Больше в промпт классификатора не отдаём, категорию видно по первым фразам.
MAX_QUERY_CHARS = 1500


def _check_token(token: str) -> None:
    """Ссылку на карточку кладём в закрытый канал, там же и токен."""
    expected = settings.operator_token
    if expected and token != expected:
        raise HTTPException(status_code=403, detail="нужен токен доступа")


def _ticket(db, incoming_id: int) -> dict:
    row = db.execute(text("""
        SELECT id, source, client_id, name, client_email,
               first_message_text, first_message_at, slack_thread_ts
        FROM incoming_messages WHERE id = :id
    """), {"id": incoming_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="обращение не найдено")
    return {
        "incoming_id": row.id,
        "source": row.source,
        "client_id": row.client_id,
        "name": row.name,
        "client_email": row.client_email,
        "text": row.first_message_text or "",
        "started_at": row.first_message_at,
        "slack_thread_ts": row.slack_thread_ts,
    }


def _known_side(db, source: str, client_id: str) -> str | None:
    """Заказчик или исполнитель. По прошлым разборам этого же чата."""
    row = db.execute(text("""
        SELECT a.side
        FROM dialogs d
        JOIN analysis_results a ON a.dialog_id = d.id
        WHERE d.source = :src AND d.client_id = :cid AND a.side IS NOT NULL
        ORDER BY d.id DESC LIMIT 1
    """), {"src": source, "cid": client_id}).first()
    return row[0] if row else None


def _guess_category(message: str, side: str | None) -> str | None:
    """Категория свежего обращения тем же классификатором, что и ночная джоба."""
    result, _ = ai_analysis._call_openai(message)
    if not result:
        return None
    category = (result.get("category") or "").strip()
    if not category:
        return None
    return ai_analysis._validate_category(side, category) if side else category


def _history(db, source: str, client_id: str, limit: int) -> list[dict]:
    """С чем этот чат писал раньше, свежее сверху. Без теневых дублей."""
    # DISTINCT ON: у диалога бывает несколько разборов, берём самый свежий.
    rows = db.execute(text("""
        SELECT * FROM (
            SELECT DISTINCT ON (d.id)
                   d.id,
                   COALESCE(d.dialog_date, d.created_at::date) AS dt,
                   a.category, a.subcategory, a.summary, a.resolution, o.outcome
            FROM dialogs d
            JOIN analysis_results a ON a.dialog_id = d.id
            LEFT JOIN dialog_outcomes o ON o.dialog_id = d.id
            WHERE d.source = :src AND d.client_id = :cid
              AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                              WHERE s.dialog_id = d.id)
            ORDER BY d.id DESC, a.id DESC
        ) x
        ORDER BY dt DESC NULLS LAST, id DESC
        LIMIT :lim
    """), {"src": source, "cid": client_id, "lim": limit}).fetchall()
    return [
        {
            "dialog_id": r.id,
            "date": r.dt.isoformat() if r.dt else None,
            "category": r.category,
            "subcategory": r.subcategory,
            "summary": r.summary,
            "resolution": r.resolution,
            "outcome": r.outcome,
        }
        for r in rows
    ]


@router.get("/card")
def card(id: int = Query(..., description="incoming_messages.id"),
         token: str = Query("")) -> dict:
    _check_token(token)

    with get_session() as db:
        ticket = _ticket(db, id)
        source, client_id = ticket["source"], ticket["client_id"]
        query = (ticket["text"] or "").strip()[:MAX_QUERY_CHARS]

        side = _known_side(db, source, client_id)
        category = _guess_category(query, side) if query else None

        similar = similar_cases.find(
            db, query, category=category, side=side, limit=SIMILAR_LIMIT,
        ) if query else []

        email, how = operator_profile.resolve_executor_email(db, source, client_id)
        history = _history(db, source, client_id, HISTORY_LIMIT)

    executor: dict = {"email": email, "resolved_by": how}
    if email:
        executor.update(operator_profile.profile(email))

    return {
        "ticket": ticket | {"side": side, "category": category},
        "similar": similar,
        "executor": executor,
        "history": history,
    }
