"""
ChatApp API client — STRICT WHITELIST.

Разрешены ровно 5 методов (зафиксировано в _ALLOWED). Любой другой URL/метод
немедленно поднимает RuntimeError ещё до сетевого вызова — это сделано
намеренно, чтобы случайное расширение клиента нельзя было выкатить без
явного изменения whitelist.

Все вызовы автоматически добавляют заголовок:
    Authorization: <accessToken>   (raw, без префикса Bearer)

Токены кэшируются в таблице chatapp_tokens (одна строка с id=1):
    — за 5 минут до истечения accessToken пробуем refresh
    — если refresh упал — один раз перелогиниваемся через /v1/tokens
Лимит логинов: 100/сутки на email-appId, поэтому login — крайняя мера.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from config import settings
from database import ChatappToken, get_session


log = logging.getLogger(__name__)

# (method, path_template) — пути в нотации с :param, как в Postman.
_ALLOWED: set[tuple[str, str]] = {
    ("POST", "/v1/tokens"),
    ("POST", "/v1/tokens/refresh"),
    ("GET",  "/v1/tokens/check"),
    ("GET",  "/v1/licenses/:licenseId/messengers/:messengerType/chats"),
    ("GET",  "/v1/licenses/:licenseId/messengers/:messengerType/chats/:chatId/messages"),
}

# За сколько секунд до истечения accessToken пробуем refresh.
_REFRESH_LEEWAY_SEC = 5 * 60

# Таймаут HTTP-запросов.
_TIMEOUT_SEC = 30


class ChatAppError(RuntimeError):
    """Любая ошибка работы с ChatApp API."""


def _assert_allowed(method: str, path_template: str) -> None:
    if (method, path_template) not in _ALLOWED:
        raise RuntimeError(
            f"ChatApp client: method {method} {path_template} is NOT in whitelist. "
            f"Allowed: {sorted(_ALLOWED)}"
        )


def _now() -> int:
    return int(time.time())


# ── Token storage ────────────────────────────────────────────────────────────

def _load_token() -> ChatappToken | None:
    with get_session() as db:
        return db.get(ChatappToken, 1)


def _save_token(payload: dict[str, Any]) -> None:
    """payload = data из ответа /v1/tokens или /v1/tokens/refresh."""
    with get_session() as db:
        row = db.get(ChatappToken, 1)
        if row is None:
            row = ChatappToken(id=1)
            db.add(row)
        row.cabinet_user_id = payload.get("cabinetUserId")
        row.access_token = payload["accessToken"]
        row.access_token_end_time = int(payload["accessTokenEndTime"])
        row.refresh_token = payload["refreshToken"]
        row.refresh_token_end_time = int(payload["refreshTokenEndTime"])
        db.commit()


# ── Low-level HTTP ───────────────────────────────────────────────────────────

def _request(
    method: str,
    path_template: str,
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Низкоуровневый вызов: подставляет path-params, добавляет Authorization."""
    _assert_allowed(method, path_template)

    path = path_template
    for key, val in (path_params or {}).items():
        placeholder = f":{key}"
        if placeholder not in path:
            raise RuntimeError(f"path_template {path_template} не содержит {placeholder}")
        path = path.replace(placeholder, str(val))

    url = settings.chatapp_base_url.rstrip("/") + path
    headers = {"Accept": "application/json", "Lang": "en"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if auth_token:
        headers["Authorization"] = auth_token

    try:
        resp = httpx.request(
            method, url,
            params=query,
            json=json_body,
            headers=headers,
            timeout=_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise ChatAppError(f"HTTP transport error {method} {path}: {exc}") from exc

    try:
        data = resp.json()
    except Exception as exc:
        raise ChatAppError(
            f"Non-JSON response {method} {path} status={resp.status_code}: {resp.text[:200]}"
        ) from exc

    if resp.status_code >= 400 or not data.get("success", False):
        raise ChatAppError(
            f"API error {method} {path} status={resp.status_code} body={data}"
        )
    return data.get("data", {})


# ── Auth flow ────────────────────────────────────────────────────────────────

def _login() -> str:
    """POST /v1/tokens — полный логин по email/password/appId. Кэширует токены."""
    if not (settings.chatapp_email and settings.chatapp_password and settings.chatapp_app_id):
        raise ChatAppError(
            "ChatApp credentials not set: CHATAPP_EMAIL / CHATAPP_PASSWORD / CHATAPP_APP_ID"
        )
    log.info("ChatApp: login via /v1/tokens (email=%s appId=%s)",
             settings.chatapp_email, settings.chatapp_app_id)
    data = _request(
        "POST", "/v1/tokens",
        json_body={
            "email": settings.chatapp_email,
            "password": settings.chatapp_password,
            "appId": settings.chatapp_app_id,
        },
    )
    _save_token(data)
    return data["accessToken"]


def _refresh(refresh_token: str) -> str:
    """POST /v1/tokens/refresh."""
    log.info("ChatApp: refresh access token via /v1/tokens/refresh")
    data = _request(
        "POST", "/v1/tokens/refresh",
        json_body={"refreshToken": refresh_token},
    )
    _save_token(data)
    return data["accessToken"]


def get_access_token() -> str:
    """Вернуть валидный accessToken (из кэша / refresh / login)."""
    row = _load_token()
    now = _now()

    if row and row.access_token_end_time > now + _REFRESH_LEEWAY_SEC:
        return row.access_token

    if row and row.refresh_token_end_time > now:
        try:
            return _refresh(row.refresh_token)
        except ChatAppError as exc:
            log.warning("ChatApp refresh failed (%s), falling back to login.", exc)

    return _login()


def check_token() -> dict[str, Any]:
    """GET /v1/tokens/check — диагностика."""
    token = get_access_token()
    return _request("GET", "/v1/tokens/check", auth_token=token)


# ── Public read methods (whitelisted) ────────────────────────────────────────

def list_chats(
    license_id: str,
    messenger_type: str,
    *,
    limit: int = 20,
    last_time: int | None = None,
) -> dict[str, Any]:
    """
    GET /v1/licenses/{licenseId}/messengers/{messengerType}/chats

    Возвращает {items: [...], nextPage: "..."} (как в Postman).
    last_time — unix seconds для пагинации (берётся из последнего чата страницы).
    """
    token = get_access_token()
    query: dict[str, Any] = {"limit": limit}
    if last_time is not None:
        query["lastTime"] = int(last_time)
    return _request(
        "GET",
        "/v1/licenses/:licenseId/messengers/:messengerType/chats",
        path_params={"licenseId": license_id, "messengerType": messenger_type},
        query=query,
        auth_token=token,
    )


def list_messages(
    license_id: str,
    messenger_type: str,
    chat_id: str,
    *,
    limit: int = 20,
    direction: str = "prev",
    last_time: int | None = None,
    last_internal_id: str | int | None = None,
    next_page: str | None = None,
) -> dict[str, Any]:
    """
    GET /v1/licenses/{licenseId}/messengers/{messengerType}/chats/{chatId}/messages

    direction: "prev" (от новых к старым) | "next" (от старых к новым).
    Пагинация: либо (last_time + last_internal_id), либо next_page (предпочтительно).
    """
    if direction not in ("prev", "next"):
        raise ValueError(f"direction must be 'prev' or 'next', got {direction!r}")
    token = get_access_token()
    query: dict[str, Any] = {"limit": limit, "direction": direction}
    if next_page is not None:
        query["nextPage"] = next_page
    else:
        if last_time is not None:
            query["lastTime"] = int(last_time)
        if last_internal_id is not None:
            query["lastInternalId"] = str(last_internal_id)
    return _request(
        "GET",
        "/v1/licenses/:licenseId/messengers/:messengerType/chats/:chatId/messages",
        path_params={
            "licenseId": license_id,
            "messengerType": messenger_type,
            "chatId": chat_id,
        },
        query=query,
        auth_token=token,
    )
