"""
Natural-language DB query endpoint (used by the /test UI 'DB Query' tab).
Pipes the question through the db_query.answer() pipeline.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from tools import db_query
router = APIRouter()


class DBQueryRequest(BaseModel):
    question: str


@router.post("/api/db-query")
def api_db_query(body: DBQueryRequest) -> dict:
    return db_query.answer(body.question)
