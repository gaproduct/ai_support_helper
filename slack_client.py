"""Slack chat.postMessage wrapper."""

import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)


def post_slack(
    text: str,
    thread_ts: str | None = None,
    channel: str | None = None,
    blocks: list[dict] | None = None,
) -> str | None:
    """
    Post a message to a Slack channel.

    - `channel` — если не задан, используется settings.slack_channel_id.
    - `thread_ts` — если задан, постится как reply в треде.
    - `blocks` — опциональный Block Kit (для интерактивных сообщений с кнопками).
                 `text` при этом остаётся как fallback для уведомлений.

    Returns the ts of the posted message, or None on error.
    """
    if not settings.slack_bot_token:
        return None
    payload: dict[str, Any] = {
        "channel": channel or settings.slack_channel_id,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data["ts"]
        log.error("Slack post error: %s", data.get("error"))
    except Exception as exc:
        log.error("Slack post exception: %s", exc)
    return None


def update_slack(
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict] | None = None,
) -> bool:
    """chat.update — обновить ранее опубликованное сообщение по (channel, ts)."""
    if not settings.slack_bot_token:
        return False
    payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return True
        log.error("Slack chat.update error: %s", data.get("error"))
    except Exception as exc:
        log.error("Slack chat.update exception: %s", exc)
    return False


def get_thread_replies(channel: str, thread_ts: str, limit: int = 50) -> list[dict]:
    """Fetch reply messages of a Slack thread via conversations.replies."""
    if not settings.slack_bot_token:
        return []
    try:
        resp = httpx.get(
            "https://slack.com/api/conversations.replies",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            params={"channel": channel, "ts": thread_ts, "limit": limit},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("messages", []) or []
        log.error("Slack conversations.replies error: %s", data.get("error"))
    except Exception as exc:
        log.error("Slack conversations.replies exception: %s", exc)
    return []


def build_permalink(channel: str, ts: str) -> str | None:
    """Get a permalink for a Slack message via chat.getPermalink."""
    if not settings.slack_bot_token:
        return None
    try:
        resp = httpx.get(
            "https://slack.com/api/chat.getPermalink",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            params={"channel": channel, "message_ts": ts},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("permalink")
        log.error("Slack chat.getPermalink error: %s", data.get("error"))
    except Exception as exc:
        log.error("Slack chat.getPermalink exception: %s", exc)
    return None
