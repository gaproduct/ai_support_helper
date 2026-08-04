"""
ChatApp history backfill job.

Тянет все чаты + сообщения за период [chatapp_history_from .. сейчас]
и складывает в таблицу chatapp_dialogs. Существующие таблицы (dialogs,
incoming_messages, scenario_drafts, analysis_results) не трогаются.

Запуск:
    docker exec -it support_tickets-webhook-1 python -m chatapp_history

Алгоритм (на каждый messengerType из CHATAPP_MESSENGER_TYPES):
  1. Идём по /chats с курсором lastTime от «сейчас» вниз.
  2. Останавливаемся, когда последний чат страницы старше cutoff (history_from).
  3. Для каждого чата с активностью >= cutoff:
     /chats/{chatId}/messages?direction=next&lastTime=<cutoff_unix>
     с пагинацией по nextPage. Собираем все сообщения за период.
  4. Upsert в chatapp_dialogs: уникальный ключ (license_id, messenger_type, chat_id).
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from chatapp_client import (
    ChatAppError,
    get_employee_roster,
    list_chats,
    list_messages,
)
from config import settings
from database import Dialog, get_session


log = logging.getLogger(__name__)


# ── Time helpers ─────────────────────────────────────────────────────────────

def _parse_iso_to_unix(iso: str) -> int:
    """ISO-8601 → unix seconds (UTC)."""
    s = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _unix_to_iso(ts: int | float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# ── Message mapping ──────────────────────────────────────────────────────────

def _msg_text(msg: dict[str, Any]) -> str:
    """Достаём текстовое представление сообщения (text > caption > имя файла)."""
    m = msg.get("message") or {}
    text = (m.get("text") or "").strip()
    if text:
        return text
    caption = (m.get("caption") or "").strip()
    if caption:
        return caption
    f = m.get("file") or {}
    if f.get("name"):
        return f"[{msg.get('type','file')}: {f['name']}]"
    return f"[{msg.get('type','?')}]"


def _map_message(msg: dict[str, Any]) -> dict[str, Any]:
    """ChatApp message → нормализованный JSON-элемент.

    Поля сделаны совместимыми с существующей таблицей dialogs (direction/time/text),
    чтобы потом проще было смерджить.

    Для outbound сообщений `fromUser` всегда общий бизнес-аккаунт (RemozoSupport),
    поэтому реального оператора достаём из `created.id` и резолвим в email через
    роутер сотрудников компании. Автоматические сообщения бота (fromApp.sender ==
    "system") помечаем operator_is_bot=True и не приписываем живому оператору.
    """
    side = msg.get("side", "")
    direction = "outbound" if side == "out" else "inbound"
    from_user = msg.get("fromUser") or {}
    from_app = msg.get("fromApp") or {}
    app_sender = from_app.get("sender") or ""

    author = from_user.get("name") or ""
    operator_id = ""
    operator_email = ""
    operator_is_bot = False
    if direction == "outbound":
        created = msg.get("created") or {}
        operator_id = str(created.get("id") or "")
        operator_is_bot = app_sender == "system"
        if operator_id:
            info = get_employee_roster().get(operator_id) or {}
            operator_email = info.get("email") or ""
            # Живого оператора выносим в author; бота оставляем под общим именем.
            if operator_email and not operator_is_bot:
                author = operator_email

    return {
        "text":      _msg_text(msg),
        "direction": direction,
        "time":      _unix_to_iso(msg.get("time")),
        "time_unix": msg.get("time"),
        "type":      msg.get("type", ""),
        "author":    author,
        "author_id": from_user.get("id") or "",
        "operator_id":    operator_id,
        "operator_email": operator_email,
        "operator_is_bot": operator_is_bot,
        "app_sender":     app_sender,
        "phone":     from_user.get("phone") or "",
        "email":     from_user.get("email") or "",
        "message_id": msg.get("id") or "",
        "internal_id": msg.get("internalId") or "",
    }


# ── Chat helpers ─────────────────────────────────────────────────────────────

def _chat_last_time(chat: dict[str, Any]) -> int:
    """Время последней активности чата в unix seconds."""
    # У ChatApp в /chats обычно поле time / lastMessageTime / updatedAt.
    for key in ("time", "lastMessageTime", "updatedAt", "lastTime"):
        v = chat.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _chat_meta(chat: dict[str, Any]) -> dict[str, Any]:
    """Достаём из объекта чата ID / name / phone / email."""
    chat_id = (
        chat.get("id")
        or chat.get("chatId")
        or (chat.get("user") or {}).get("id")
        or ""
    )
    user = chat.get("user") or chat.get("fromUser") or {}
    return {
        "chat_id": str(chat_id),
        "chat_name": chat.get("name") or user.get("name") or "",
        "phone": user.get("phone") or chat.get("phone") or "",
        "email": user.get("email") or chat.get("email") or "",
    }


# ── Backfill core ────────────────────────────────────────────────────────────

def _fetch_messages_for_chat(
    license_id: str,
    messenger_type: str,
    chat_id: str,
    cutoff_unix: int,
) -> list[dict[str, Any]]:
    """Все сообщения чата начиная с cutoff_unix (direction=next, paginate by nextPage)."""
    all_msgs: list[dict[str, Any]] = []
    next_page: str | None = None
    last_time: int | None = cutoff_unix
    last_internal_id: str | None = None
    page = 0

    while True:
        page += 1
        try:
            data = list_messages(
                license_id, messenger_type, chat_id,
                limit=100,
                direction="next",
                last_time=last_time if next_page is None else None,
                last_internal_id=last_internal_id if next_page is None else None,
                next_page=next_page,
            )
        except ChatAppError as exc:
            log.error("messages fetch failed chat=%s page=%d: %s", chat_id, page, exc)
            break

        items = data.get("items") or []
        all_msgs.extend(items)
        log.info("  chat=%s page=%d got %d messages (total=%d)",
                 chat_id, page, len(items), len(all_msgs))

        next_page = data.get("nextPage")
        if not next_page or not items:
            break
        # Hard safety: не больше 200 страниц на чат (20k сообщений).
        if page >= 200:
            log.warning("  chat=%s: hit 200-page cap, stopping pagination", chat_id)
            break

    return all_msgs


def _flatten_messages(items: list[dict[str, Any]]) -> str:
    """Deprecated. Сейчас messages_text хранит тот же JSON, что и messages_json.

    Оставлено в коде для возможной отладочной выгрузки человекочитаемого вида
    (CLI/log). В БД больше не пишем.
    """
    lines: list[str] = []
    for m in items:
        ts = m.get("time") or ""
        direction = m.get("direction") or ""
        label = "Поддержка" if direction == "outbound" else "Клиент"
        body = (m.get("text") or "").strip()
        lines.append(f"[{ts}] {label}: {body}")
    return "\n".join(lines)


def _upsert_chat_dialog(
    license_id: str,
    messenger_type: str,
    chat_meta: dict[str, Any],
    messages: list[dict[str, Any]],
) -> None:
    """Сохранить/обновить ОТДЕЛЬНО за каждый UTC-день, в котором были сообщения.

    Пишем в общую таблицу dialogs с source='chatapp'. Уникальный ключ —
    partial UNIQUE (license_id, messenger_type, client_id, dialog_date) WHERE source='chatapp'.
    На один прогон бэкфилла чат может породить N строк (по числу активных дней).
    """
    if not messages:
        return
    mapped = [_map_message(m) for m in messages]

    # Группируем по UTC-дате (из time_unix).
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in mapped:
        ts = m.get("time_unix")
        if not ts:
            continue
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        by_day[day].append(m)

    if not by_day:
        log.warning("  chat=%s: messages without time, skipped", chat_meta["chat_id"])
        return

    chat_id = chat_meta["chat_id"]

    with get_session() as db:
        for day in sorted(by_day.keys()):
            items = sorted(by_day[day], key=lambda x: x.get("time_unix") or 0)
            started = items[0].get("time") or ""
            finished = items[-1].get("time") or ""
            # Унифицированный формат: messages_text и messages_json держим одинаковыми
            # (JSON-array объектов сообщений), чтобы экспорты не зависели от source.
            messages_json = json.dumps(items, ensure_ascii=False)
            messages_text = messages_json

            existing = (
                db.query(Dialog)
                .filter(
                    Dialog.source == "chatapp",
                    Dialog.license_id == license_id,
                    Dialog.messenger_type == messenger_type,
                    Dialog.client_id == chat_id,
                    Dialog.dialog_date == day,
                )
                .first()
            )
            if existing is None:
                db.add(Dialog(
                    client_id=chat_id,
                    source="chatapp",
                    messages_text=messages_text,
                    messages_json=messages_json,
                    started_at=started,
                    finished_at=finished,
                    processed=False,
                    license_id=license_id,
                    messenger_type=messenger_type,
                    dialog_date=day,
                    chat_name=chat_meta.get("chat_name") or None,
                    phone=chat_meta.get("phone") or None,
                    email=chat_meta.get("email") or None,
                    messages_count=len(items),
                ))
                log.info("  inserted chat=%s day=%s msgs=%d",
                         chat_id, day, len(items))
            else:
                existing.chat_name = chat_meta.get("chat_name") or existing.chat_name
                existing.phone = chat_meta.get("phone") or existing.phone
                existing.email = chat_meta.get("email") or existing.email
                existing.started_at = started
                existing.finished_at = finished
                existing.messages_count = len(items)
                existing.messages_json = messages_json
                existing.messages_text = messages_text
                log.info("  updated  chat=%s day=%s msgs=%d",
                         chat_id, day, len(items))
        db.commit()


def backfill_messenger(license_id: str, messenger_type: str, cutoff_unix: int) -> None:
    log.info("=== Backfill license=%s messenger=%s cutoff=%s (unix=%d) ===",
             license_id, messenger_type, _unix_to_iso(cutoff_unix), cutoff_unix)

    last_time: int | None = None  # начинаем с «сейчас» — API сам отдаст самые свежие
    page = 0
    total_chats = 0
    total_processed = 0

    while True:
        page += 1
        try:
            data = list_chats(license_id, messenger_type, limit=100, last_time=last_time)
        except ChatAppError as exc:
            log.error("chats fetch failed page=%d: %s", page, exc)
            break

        items = data.get("items") or []
        if not items:
            log.info("page=%d empty, stop.", page)
            break

        total_chats += len(items)
        log.info("page=%d got %d chats (total=%d)", page, len(items), total_chats)

        stop = False
        for chat in items:
            chat_time = _chat_last_time(chat)
            meta = _chat_meta(chat)
            if not meta["chat_id"]:
                log.warning("  skip chat without id: %r", chat)
                continue
            if chat_time and chat_time < cutoff_unix:
                # Чаты идут по убыванию активности — как только встретили старый, дальше тоже старые.
                stop = True
                continue
            log.info(" → chat %s (%s) lastActivity=%s",
                     meta["chat_id"], meta.get("chat_name") or "-",
                     _unix_to_iso(chat_time))
            msgs = _fetch_messages_for_chat(
                license_id, messenger_type, meta["chat_id"], cutoff_unix
            )
            if msgs:
                _upsert_chat_dialog(license_id, messenger_type, meta, msgs)
                total_processed += 1

        if stop:
            log.info("encountered chat older than cutoff, stopping outer loop.")
            break

        # Курсор на следующую страницу — берём минимальный lastTime из этой пачки.
        page_times = [_chat_last_time(c) for c in items if _chat_last_time(c)]
        if not page_times:
            break
        next_cursor = min(page_times)
        if last_time is not None and next_cursor >= last_time:
            log.warning("pagination cursor did not advance, stopping.")
            break
        last_time = next_cursor

        if page >= 100:
            log.warning("hit 100-page cap on chats list, stopping.")
            break

    log.info("=== Done license=%s messenger=%s: %d chats scanned, %d processed ===",
             license_id, messenger_type, total_chats, total_processed)


def _resolve_messenger_types() -> list[str]:
    if not settings.chatapp_license_id:
        raise SystemExit("CHATAPP_LICENSE_ID is not set")
    if not settings.chatapp_messenger_types.strip():
        raise SystemExit("CHATAPP_MESSENGER_TYPES is not set (comma-separated list)")
    return [m.strip() for m in settings.chatapp_messenger_types.split(",") if m.strip()]


def run() -> None:
    """Полный backfill от настройки chatapp_history_from до now. Ручной запуск."""
    cutoff_unix = _parse_iso_to_unix(settings.chatapp_history_from)
    messenger_types = _resolve_messenger_types()

    log.info("ChatApp backfill: license=%s messengers=%s from=%s",
             settings.chatapp_license_id, messenger_types,
             settings.chatapp_history_from)

    for mt in messenger_types:
        try:
            backfill_messenger(settings.chatapp_license_id, mt, cutoff_unix)
        except Exception:
            log.exception("backfill_messenger failed for %s", mt)


def run_daily(lookback_hours: int = 25) -> None:
    """Инкрементальный daily-job: окно (now - lookback_hours) .. now.

    25 часов по умолчанию — на 1 час перекрытия с предыдущим прогоном на случай
    задержек ChatApp / уплыва часов. Идемпотентно: upsert по
    (license_id, messenger_type, chat_id, dialog_date).
    """
    messenger_types = _resolve_messenger_types()
    now_unix = int(datetime.now(timezone.utc).timestamp())
    cutoff_unix = now_unix - lookback_hours * 3600

    log.info("ChatApp daily backfill: license=%s messengers=%s window=[%s, %s)",
             settings.chatapp_license_id, messenger_types,
             _unix_to_iso(cutoff_unix), _unix_to_iso(now_unix))

    for mt in messenger_types:
        try:
            backfill_messenger(settings.chatapp_license_id, mt, cutoff_unix)
        except Exception:
            log.exception("backfill_messenger failed for %s", mt)


if __name__ == "__main__":
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run()
