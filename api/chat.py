"""Chat session endpoints (used by /chat and /slack emulators)."""

from fastapi import APIRouter
from pydantic import BaseModel

import chat_session

router = APIRouter()


class ChatMessageRequest(BaseModel):
    session_id: str
    message: str


@router.post("/api/chat")
def api_chat(body: ChatMessageRequest) -> dict:
    return chat_session.handle_message(body.session_id, body.message)


@router.post("/api/chat/reset")
def api_chat_reset(body: dict) -> dict:
    chat_session.reset(body.get("session_id", ""))
    return {"status": "ok"}
