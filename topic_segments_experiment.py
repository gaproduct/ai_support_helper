"""
ЭКСПЕРИМЕНТ (в стороне от продакшена): нарезка переписки на тикеты по СМЕНЕ ТЕМЫ,
а не по фиксированной паузе. Ничего в существующих таблицах/модулях не меняем —
переиспользуем resolution_metrics.normalize / compute_incident, заменяя только
логику сегментации.

Границы тикета (детерминированно, без LLM):
  B1 greeting     — клиентское сообщение начинается с приветствия, а в текущем
                    сегменте уже есть содержательное — это новое обращение;
  B2 entity       — клиент вводит НОВУЮ сущность (email исполнителя / номер карты),
                    отличную от уже установленной в сегменте — другой предмет запроса;
  B3 close->new   — в сегменте уже был резолв/закрытие, после чего клиент пишет снова;
  B4 pause        — вспомогательный: пауза перед клиентским сообщением превышает порог
                    (с поправкой на выходные: пятница→понедельник → до 72ч).

Границу двигает ТОЛЬКО содержательное клиентское сообщение. Ответы поддержки и
заглушки всегда прилипают к текущему сегменту.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import resolution_metrics as rm

# ── сигналы границы ──────────────────────────────────────────────────────────

_GREETING_START = re.compile(
    r"^\W*(здравствуй|добр(ый|ого) (день|дня|вечер|вечера|утро|утра)|"
    r"доброе утро|доброго времени|привет\w*|good (morning|day|afternoon|evening)|"
    r"hello|hi)\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# карта: 16 цифр группами, допускаем маскировку * и разделители
_CARD_RE = re.compile(r"\b\d{4}[\s\-\*x]{0,4}\d{2,4}[\s\-\*x]{0,4}\d{2,4}[\s\-\*x]{0,4}\d{2,4}\b")

BASE_GAP_HOURS = 24.0
WEEKEND_GAP_HOURS = 72.0


def _entities(text: str) -> set[str]:
    ents = set(m.group(0).lower() for m in _EMAIL_RE.finditer(text))
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 12:
            ents.add("card:" + digits[-4:])
    return ents


def _weekend_between(a: datetime, b: datetime) -> bool:
    """Есть ли суббота/воскресенье в интервале (a, b] по МСК."""
    cur = a
    while cur <= b:
        if cur.astimezone(rm.MSK).weekday() >= 5:
            return True
        cur += timedelta(hours=6)
    return False


def _pause_exceeded(prev_ts: datetime, cur_ts: datetime) -> bool:
    gap_h = (cur_ts - prev_ts).total_seconds() / 3600.0
    thr = WEEKEND_GAP_HOURS if _weekend_between(prev_ts, cur_ts) else BASE_GAP_HOURS
    return gap_h > thr


def _seg_has_resolution_or_close(seg: list) -> bool:
    """Достиг ли сегмент решения/закрытия к текущему моменту."""
    last_sub = next((m for m in reversed(seg) if m.substantive), None)
    if last_sub is None:
        return False
    if last_sub.is_support and rm._is_resolution(last_sub.text, True):
        return True
    if last_sub.is_client and (
        rm._matches(rm._CLIENT_CLOSE, last_sub.text)
        or rm._matches(rm._CLIENT_NEG_CLOSE, last_sub.text)
    ):
        return True
    return False


def segment_by_topic(msgs: list) -> list[list]:
    """Режем нормализованные сообщения на тикеты по смене темы."""
    if not msgs:
        return []
    segments: list[list] = [[msgs[0]]]
    seg_entities: set[str] = set(_entities(msgs[0].text)) if msgs[0].substantive else set()

    for prev, cur in zip(msgs, msgs[1:]):
        cur_seg = segments[-1]
        boundary = False
        reason = None

        if cur.is_client and cur.substantive:
            has_prior_sub = any(m.substantive for m in cur_seg)
            cur_ents = _entities(cur.text)

            # B1 greeting
            if has_prior_sub and _GREETING_START.search(cur.text.strip()):
                boundary, reason = True, "greeting"
            # B2 entity change
            elif seg_entities and cur_ents and not (cur_ents & seg_entities):
                boundary, reason = True, "entity"
            # B3 close -> new
            elif has_prior_sub and _seg_has_resolution_or_close(cur_seg):
                boundary, reason = True, "close_new"
            # B4 pause (weekend-aware)
            elif _pause_exceeded(prev.ts, cur.ts):
                boundary, reason = True, "pause"

        # B5 post-close: сегмент уже закрыт (резолв/закрытие) и после длинной
        # паузы приходит ЛЮБОЕ сообщение (в т.ч. поздний ответ саппорта по
        # другому тикету) — отрезаем, чтобы не сдвигать resolved уже закрытого.
        if not boundary and _pause_exceeded(prev.ts, cur.ts) \
                and _seg_has_resolution_or_close(cur_seg):
            boundary, reason = True, "post_close_pause"

        if boundary:
            segments.append([cur])
            seg_entities = set(_entities(cur.text))
            cur._boundary_reason = reason  # для отладки
        else:
            cur_seg.append(cur)
            if cur.is_client and cur.substantive:
                seg_entities |= _entities(cur.text)

    return segments


def analyze_by_topic(raw_messages) -> list:
    msgs = rm.normalize(raw_messages)
    out = []
    for idx, seg in enumerate(segment_by_topic(msgs)):
        if not any(m.is_client and m.substantive for m in seg):
            continue
        out.append((idx, seg, rm.compute_incident(seg, idx)))
    return out
