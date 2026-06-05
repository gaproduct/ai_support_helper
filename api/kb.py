"""KB articles listing endpoint (used by /kb UI)."""

import httpx
from fastapi import APIRouter

from core.config import settings

router = APIRouter()


@router.get("/api/kb-articles")
def api_kb_articles() -> dict:
    """Fetch all Q&A articles from Supabase kb_articles."""
    if not settings.supabase_url or not settings.supabase_key:
        return {"articles": [], "error": "Supabase не настроен"}
    try:
        resp = httpx.get(
            f"{settings.supabase_url}/rest/v1/kb_articles",
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {settings.supabase_key}",
            },
            params={"select": "id,question,answer", "order": "id.asc"},
            timeout=10,
        )
        resp.raise_for_status()
        return {"articles": resp.json()}
    except Exception as exc:
        return {"articles": [], "error": str(exc)}
