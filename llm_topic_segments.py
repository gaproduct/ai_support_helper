"""
ЭКСПЕРИМЕНТ: разметка границ тикетов внутри сшитого диалога через LLM.

Проблема детерминированных правил (приветствие/сущность/пауза) — они не видят
СМЫСЛ и склеивают/режут запросы неверно. Здесь отдаём весь пронумерованный
диалог модели и просим вернуть индексы сообщений, с которых начинается новый
тикет (отдельный запрос). Продакшн не трогаем — переиспользуем normalize и
compute_incident из resolution_metrics.
"""
from __future__ import annotations

import json
import re

from openai import OpenAI

import resolution_metrics as rm
from config import settings

_client = OpenAI(api_key=settings.openai_api_key)
LLM_MODEL = "gpt-4.1-mini"

_SYSTEM = (
    "Ты аналитик поддержки B2B-сервиса выплат (MadeTask/Remozo). Тебе дают "
    "переписку одного клиента с поддержкой в виде пронумерованных сообщений. "
    "Внутри переписки может быть НЕСКОЛЬКО разных обращений (тикетов): разные "
    "вопросы, разные задачи/выплаты/исполнители, разные периоды. Твоя задача — "
    "разбить переписку на отдельные тикеты.\n"
    "Тикет — это один предмет запроса от возникновения до закрытия. Новый вопрос "
    "по ДРУГОЙ задаче/выплате/теме — новый тикет, даже если пауза маленькая. "
    "Уточнения, дозапросы данных и ответы в рамках того же предмета — тот же "
    "тикет, даже если между ними дни.\n"
    "Верни СТРОГО JSON: {\"starts\": [индексы сообщений, с которых начинается "
    "новый тикет]}. Индекс 0 всегда входит (начало первого тикета). Границы "
    "ставь только на сообщениях КЛИЕНТА."
)


def _transcript(msgs: list) -> str:
    lines = []
    for i, m in enumerate(msgs):
        role = "КЛИЕНТ" if m.is_client else "САППОРТ"
        t = (m.text or "").replace("\n", " ").strip()[:280]
        lines.append(f"[{i}] {m.ts.strftime('%Y-%m-%d %H:%M')} {role}: {t}")
    return "\n".join(lines)


def llm_starts(msgs: list, model: str = LLM_MODEL) -> list[int]:
    """Вернуть отсортированные индексы начала тикетов (0 всегда включён)."""
    if not msgs:
        return []
    resp = _client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _transcript(msgs)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        starts = json.loads(raw).get("starts", [])
    except Exception:
        nums = re.findall(r"\d+", raw)
        starts = [int(n) for n in nums]
    starts = sorted({i for i in starts if isinstance(i, int) and 0 <= i < len(msgs)})
    if not starts or starts[0] != 0:
        starts = [0] + starts
    return starts


def segment_by_llm(msgs: list, model: str = LLM_MODEL) -> list[list]:
    starts = llm_starts(msgs, model=model)
    segments = []
    for a, b in zip(starts, starts[1:] + [len(msgs)]):
        segments.append(msgs[a:b])
    return segments
