"""
Определение ТИКЕТА (согласовано):

Уровень 1 — первичный срез по времени:
  • идём по содержательным ВХОДЯЩИМ (клиентским) сообщениям по времени;
  • первое открывает срез с жёстким окном [t0, t0+24ч]; всё (обе стороны)
    внутри окна — этот срез;
  • следующий срез открывает первое клиентское сообщение ПОСЛЕ конца окна;
  • выходные: если t0 в зоне пт 16:00 → пн 09:00 (МСК), окно продлевается
    до понедельника 16:00 (≈72ч).
  • окно жёсткое: ответ вне окна к тикету НЕ привязывается (тикет остаётся
    незавершённым).

Уровень 2 — дробление среза на под-тикеты:
  • по разным сущностям исполнителя (email / карта / ФИО) — детерминированно;
  • по смене темы (приветствие / закрытие→новый) как дополнительный сигнал;
  • у каждого под-тикета своя дата старта и завершения.

Продакшн не трогаем: переисполь­зуем resolution_metrics.normalize / compute_incident.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

import resolution_metrics as rm
from topic_segments_experiment import (
    _entities, _GREETING_START, _seg_has_resolution_or_close,
)

# ── ФИО исполнителя (три слова с заглавной) как сущность ──────────────────────
_FIO_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ][а-яё]{2,})(?:\s+([А-ЯЁ][а-яё]{2,}))?\b"
)
_FIO_STOP = {"добрый", "здравствуйте", "приносим", "благодарим", "коллеги"}


def entities(text: str) -> set[str]:
    ents = set(_entities(text))  # email + card
    for m in _FIO_RE.finditer(text or ""):
        parts = [p for p in m.groups() if p]
        if len(parts) >= 2 and parts[0].lower() not in _FIO_STOP:
            ents.add("fio:" + " ".join(p.lower() for p in parts[:3]))
    return ents


# ── Шаг 2: дедуп дубль-пересылок клиента ─────────────────────────────────────
# Flomni иногда доставляет одно и то же клиентское сообщение дважды с разницей
# в минуты. Такие пересылки не должны порождать отдельные под-тикеты и ложные
# «Добрый день»-границы. Убираем клиентское содержательное сообщение, если его
# текст совпадает с недавним (в пределах окна) уже оставленным клиентским.
_RESEND_WINDOW_MIN = 15.0


def _norm_txt(t: str) -> str:
    return " ".join((t or "").lower().split())


def dedup_resends(msgs: list, window_min: float = _RESEND_WINDOW_MIN) -> list:
    out: list = []
    last_seen: dict[str, object] = {}  # normalized text -> ts последнего оставленного
    for m in msgs:
        if m.is_client and m.substantive:
            key = _norm_txt(m.text)
            prev_ts = last_seen.get(key)
            if prev_ts is not None and \
                    (m.ts - prev_ts).total_seconds() <= window_min * 60:
                continue  # дубль-пересылка — пропускаем
            last_seen[key] = m.ts
        out.append(m)
    return out


# ── запрос идентификатора со стороны саппорта (почта/карта/реквизит) ──────────
_ASKS_IDENTIFIER = re.compile(
    r"(электронн\w*\s+почт|укажите[^.]{0,25}почт|подскажите[^.]{0,25}почт|"
    r"с\s+котор\w*\s+вы\s+регистрир|номер\w*\s+карт|реквизит)",
    re.IGNORECASE,
)


def _last_support_asks_identifier(seg: list) -> bool:
    last_sup = next((x for x in reversed(seg) if x.is_support and x.substantive),
                    None)
    return bool(last_sup and _ASKS_IDENTIFIER.search(last_sup.text or ""))


# Клиент часто перечисляет несколько исполнителей/почт списком одним «залпом»
# (сообщения в пределах пары минут). Это ОДНО обращение, на которое саппорт даёт
# один общий ответ, — дробить его по сущностям нельзя (иначе плодятся ложные
# осиротевшие under-тикеты awaiting_support).
_ENUM_BURST_SEC = 180.0


def _in_client_burst(seg: list, cur, sec: float = _ENUM_BURST_SEC) -> bool:
    """cur идёт «залпом» следом за последним клиентским сообщением среза?"""
    prev = next((x for x in reversed(seg) if x.is_client and x.substantive), None)
    return prev is not None and (cur.ts - prev.ts).total_seconds() <= sec


# ── Уровень 1: окно среза ─────────────────────────────────────────────────────

def window_end(t0: datetime) -> datetime:
    """Конец окна среза с поправкой на выходные (пт 16:00 → пн 16:00 МСК)."""
    msk = t0.astimezone(rm.MSK)
    wd, tm = msk.weekday(), msk.time()
    in_weekend = (
        (wd == 4 and tm >= time(16, 0)) or wd in (5, 6) or (wd == 0 and tm < time(9, 0))
    )
    if in_weekend:
        days_to_mon = (7 - wd) % 7  # Fri→+3, Sat→+2, Sun→+1, Mon→0
        mon = (msk + timedelta(days=days_to_mon)).date()
        end = datetime.combine(mon, time(16, 0), tzinfo=rm.MSK)
        if end <= msk:
            end = msk + timedelta(hours=24)
        return end.astimezone(t0.tzinfo)
    return t0 + timedelta(hours=24)


def primary_slices(msgs: list) -> list[list]:
    """Режем на первичные срезы жёстким окном от клиентского сообщения."""
    slices: list[list] = []
    cur = None
    cur_end = None
    for m in msgs:
        if cur is not None and m.ts <= cur_end:
            cur.append(m)
            continue
        if m.is_client and m.substantive:
            cur = [m]
            cur_end = window_end(m.ts)
            slices.append(cur)
        # сообщения вне любого окна (поздний ответ саппорта, служебка) — отбрасываем
    return slices


# ── Уровень 2: дробление среза по сущностям/теме ──────────────────────────────

def split_by_entity(seg: list) -> list[list]:
    """Внутри среза режем на под-тикеты по смене сущности исполнителя/теме.
    Времени тут нет (весь срез ≤ окна) — только приветствие/сущность/закрытие."""
    if not seg:
        return []
    out: list[list] = [[seg[0]]]
    ents = set(entities(seg[0].text)) if seg[0].substantive else set()
    for prev, cur in zip(seg, seg[1:]):
        boundary = False
        if cur.is_client and cur.substantive:
            has_prior = any(x.substantive for x in out[-1])
            had_support = any(x.is_support and x.substantive for x in out[-1])
            resolved_prior = _seg_has_resolution_or_close(out[-1])
            cur_ents = entities(cur.text)
            # (A) приветствие открывает новый под-тикет ТОЛЬКО если прошлый уже
            #     обслужен саппортом или закрыт (иначе это дубль-пересылка /
            #     переприветствие в ещё не отвеченном запросе).
            if has_prior and _GREETING_START.search(cur.text.strip()) \
                    and (had_support or resolved_prior):
                boundary = True
            # (B) смена сущности — новый под-тикет, НО не когда клиент просто
            #     передаёт идентификатор (почта/карта) в ответ на прямой запрос
            #     саппорта, и НЕ когда это очередной элемент «залпового» списка
            #     исполнителей (перечисление в пределах пары минут = одно
            #     обращение с одним общим ответом саппорта).
            elif ents and cur_ents and not (cur_ents & ents) \
                    and not _last_support_asks_identifier(out[-1]) \
                    and not _in_client_burst(out[-1], cur):
                boundary = True
            elif has_prior and resolved_prior:
                boundary = True
        if boundary:
            out.append([cur])
            ents = set(entities(cur.text))
        else:
            out[-1].append(cur)
            if cur.is_client and cur.substantive:
                ents |= entities(cur.text)
    return out


def slice_tickets(raw_or_msgs, normalized: bool = False):
    """Полный разбор: L1 (окно) → L2 (сущность) → метрики по каждому под-тикету.
    Возвращает список (metrics, seg)."""
    msgs = raw_or_msgs if normalized else rm.normalize(raw_or_msgs)
    msgs = dedup_resends(msgs)
    result = []
    idx = 0
    for sl in primary_slices(msgs):
        for sub in split_by_entity(sl):
            # под-тикет обязан содержать реальное клиентское обращение — не
            # только хвостовой филлер («подожду»), иначе это не тикет.
            if not any(m.is_client and m.substantive and not rm._is_client_filler(m)
                       for m in sub):
                continue
            result.append((rm.compute_incident(sub, idx), sub))
            idx += 1
    return result
