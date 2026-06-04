"""
FastAPI app entry point.
Wires together: webhooks, API routers, UI routes, startup hook, request logging.
"""

import logging

from fastapi import FastAPI, Request

from api.chat import router as chat_router
from api.db_query import router as db_query_router
from api.kb import router as kb_router
from api.search import router as search_router
from api.train import router as train_router
from config import settings
from database import create_tables
from ui.routes import router as ui_router
from webhooks.flomni import router as flomni_router
from webhooks.inbound import router as inbound_router
from webhooks.slack_events import router as slack_events_router
from webhooks.slack_interactions import router as slack_interactions_router

logging.basicConfig(level=settings.log_level)
log = logging.getLogger(__name__)

app = FastAPI(title="Support Tickets — AI Assistant")


@app.on_event("startup")
def on_startup() -> None:
    create_tables()
    log.info("Database tables ensured.")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path in ("/webhook/flomni", "/webhook/inbound"):
        body = await request.body()
        log.info("WEBHOOK REQUEST path=%s body: %s",
                 request.url.path, body.decode(errors="replace")[:1000])

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive)
    return await call_next(request)


# Routers
app.include_router(flomni_router)
app.include_router(inbound_router)
app.include_router(slack_events_router)
app.include_router(slack_interactions_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(kb_router)
app.include_router(train_router)
app.include_router(db_query_router)
app.include_router(ui_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
