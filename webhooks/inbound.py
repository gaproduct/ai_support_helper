"""
Unified inbound webhook.

ONE endpoint для всех источников (Flomni / ChatApp / ...).
Внутри определяем источник по форме пейлоада и зовём соответствующий handler.

Дискриминатор (порядок проверок важен — ChatApp специфичнее):
  ChatApp: список envelope'ов где каждый имеет meta.licenseId + meta.messengerType + data: []
  Flomni:  всё остальное (массив events с receiver/content или одиночный объект)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request

from webhooks.chatapp import handle_chatapp_payload
from webhooks.flomni import handle_flomni_payload

log = logging.getLogger(__name__)

router = APIRouter()


def _looks_like_chatapp_envelope(env: Any) -> bool:
    """ChatApp envelope: dict с data:list и (опц.) meta.licenseId/messengerType."""
    if not isinstance(env, dict):
        return False
    if not isinstance(env.get("data"), list):
        return False
    meta = env.get("meta")
    if (
        isinstance(meta, dict)
        and "licenseId" in meta
        and "messengerType" in meta
    ):
        return True
    # Без meta — опознаём по характерным полям первого item из data.
    for item in env["data"]:
        if not isinstance(item, dict):
            continue
        if "side" in item and "fromUser" in item and isinstance(item.get("chat"), dict):
            return True
    return False


def detect_source(payload: Any) -> Literal["chatapp", "flomni"]:
    """Определяем источник по форме пейлоада.

    ChatApp шлёт либо массив envelope'ов [{data, meta}, ...], либо одиночный dict
    {data, meta} — поддерживаем оба.
    Flomni — массив events или одиночный объект с content/receiver.
    """
    if isinstance(payload, list) and payload:
        if _looks_like_chatapp_envelope(payload[0]):
            return "chatapp"
    elif isinstance(payload, dict):
        if _looks_like_chatapp_envelope(payload):
            return "chatapp"
    return "flomni"


@router.post("/webhook/inbound")
async def inbound_webhook(request: Request) -> dict[str, str]:
    body = await request.body()

    # Провайдеры проверяют живость URL пробным POST'ом: ChatApp шлёт пустое тело
    # и сохраняет callbackUrl только при 2xx в ответ. Поэтому на всё, что не
    # разбирается в событие, отвечаем ok, а не 400.
    try:
        payload: Any = json.loads(body)
    except ValueError:
        log.info("Inbound probe: body is not JSON (%d bytes), replying ok", len(body))
        return {"status": "ok"}

    if not isinstance(payload, (dict, list)):
        log.info("Inbound probe: JSON is %s, not an event, replying ok", type(payload).__name__)
        return {"status": "ok"}

    source = detect_source(payload)
    log.info("Inbound webhook routed to source=%s", source)

    if source == "chatapp":
        return handle_chatapp_payload(payload)
    return handle_flomni_payload(payload)
