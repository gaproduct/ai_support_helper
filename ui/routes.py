"""HTML UI routes — serve static templates from disk."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


@router.get("/test", response_class=HTMLResponse)
def test_ui() -> str:
    return _read("test.html")


@router.get("/train", response_class=HTMLResponse)
def train_ui() -> str:
    return _read("train.html")


@router.get("/kb", response_class=HTMLResponse)
def kb_ui() -> str:
    return _read("kb.html")


@router.get("/chat", response_class=HTMLResponse)
def chat_ui() -> str:
    return _read("chat.html")


@router.get("/slack", response_class=HTMLResponse)
def slack_ui() -> str:
    return _read("slack.html")
