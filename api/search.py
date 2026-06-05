"""KB search endpoint (used by /test UI).

Response shape:
  {
    "is_greeting": bool,
    "scenario": {"name": str, "notification": str, "needs_followup": bool} | None,
    "found": bool,
    "question"?: str,
    "answer"?: str,
    "similarity"?: float,
  }

Priority: greeting > scenario > KB. When a scenario matches, KB search is
skipped — operator sees only the scenario banner.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from ai import auto_response
import scenarios

router = APIRouter()


class SearchRequest(BaseModel):
    question: str


class AutoResponseTestRequest(BaseModel):
    message_text: str
    receiver_id: str = "test-user"


@router.post("/api/search")
def api_search(body: SearchRequest) -> dict:
    text = body.question or ""
    out: dict = {"is_greeting": False, "scenario": None, "found": False}

    if not text.strip():
        return out

    if auto_response._is_greeting(text):
        out["is_greeting"] = True
        return out

    detection = scenarios.detect_scenario(text)
    if detection:
        out["scenario"] = {
            "name": detection["name"],
            "notification": detection["notification"],
            "needs_followup": bool(detection.get("needs_followup")),
            "informational": bool(detection.get("informational")),
            "followup_hint": detection.get("followup_hint", ""),
        }
        return out

    result = auto_response.search(text)
    article = result.get("article")
    if article:
        out["found"] = True
        out["question"] = article.get("question", "")
        out["answer"] = article.get("answer", "")
        out["similarity"] = round(float(article.get("similarity", 0)), 3)
    return out


@router.post("/webhook/auto-response/test")
def auto_response_test(body: AutoResponseTestRequest) -> dict[str, str]:
    # Auto-response test endpoint kept for back-compat — slack post is wired
    # via webhooks/flomni → ai_response in production.
    return {"status": "ok", "message_text": body.message_text}
