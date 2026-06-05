"""
Natural-language → SQL → answer pipeline for the Superset (ClickHouse) DB.

Used by the /api/db-query endpoint. Steps:
  1. Load business-curated table specs from payouts_agent/table_specs/*.json
  2. Ask the LLM to generate a SELECT-only ClickHouse SQL statement
     answering the user's question, using those specs as context.
  3. Validate: must be SELECT/WITH ... SELECT, no DDL/DML keywords.
     Inject `LIMIT 100` if missing.
  4. Execute via payouts_agent.run_sql.
  5. Ask the LLM to summarise the result for the user.

Safety: read-only by construction. SQL is validated server-side; no
user-written SQL is ever passed to Superset.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from openai import OpenAI

import payouts_agent
from core.config import settings

log = logging.getLogger(__name__)

_TABLE_SPECS_DIR = Path(__file__).resolve().parent / "payouts_agent" / "table_specs"
_ROW_LIMIT = 100
_SQL_MODEL = "gpt-4o"

_openai_client = OpenAI(api_key=settings.openai_api_key)

# Forbidden keywords (whole-word match, case-insensitive). Anything that
# could mutate the DB or escape sandbox rejects the query.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|"
    r"replace|merge|attach|detach|optimize|rename|copy|call)\b",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)


def _load_specs() -> str:
    """Concatenate all curated table specs into a single text block for the LLM."""
    parts = []
    for path in sorted(_TABLE_SPECS_DIR.glob("*.json")):
        parts.append(f"=== {path.stem} ===")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts) if parts else "(no specs available)"


def _build_sql_prompt(question: str, specs: str) -> list[dict]:
    system = (
        "Ты ассистент по работе с ClickHouse через Superset. "
        "На вход тебе дают вопрос пользователя и описание доступных таблиц. "
        "Сгенерируй ОДИН валидный ClickHouse SELECT-запрос, отвечающий на "
        "вопрос. Запрос должен:\n"
        "  - быть только SELECT (или WITH ... SELECT), без DDL/DML;\n"
        "  - использовать полные имена таблиц вида schema.table из спецификаций;\n"
        f"  - содержать LIMIT не более {_ROW_LIMIT};\n"
        "  - не содержать комментариев и пояснений вне SQL.\n\n"
        "Верни ТОЛЬКО SQL, без markdown-обрамления и без пояснений."
    )
    user = (
        f"Доступные таблицы:\n{specs}\n\n"
        f"Вопрос пользователя:\n{question}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_summary_prompt(question: str, sql: str, rows: list[dict]) -> list[dict]:
    system = (
        "Ты помощник саппорта. На вход тебе дают вопрос, SQL-запрос и его "
        "результат в виде списка строк (JSON). Сформулируй краткий понятный "
        "ответ на вопрос пользователя на русском, ссылаясь на конкретные "
        "значения из результата. Если строк нет — так и скажи."
    )
    sample = rows[:20]
    user = (
        f"Вопрос: {question}\n\n"
        f"SQL:\n{sql}\n\n"
        f"Всего строк: {len(rows)}. Первые до 20:\n"
        f"{json.dumps(sample, ensure_ascii=False, default=str, indent=2)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove ```sql / ``` fences
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _ensure_safe_select(sql: str) -> str:
    cleaned = _strip_markdown(sql).rstrip(";").strip()
    if not cleaned:
        raise ValueError("Сгенерированный SQL пуст")

    head = cleaned.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError(f"Разрешены только SELECT-запросы, получено: {cleaned[:60]!r}")

    forbidden = _FORBIDDEN.search(cleaned)
    if forbidden:
        raise ValueError(f"В SQL запрещённое ключевое слово: {forbidden.group(0)}")

    if not _LIMIT_RE.search(cleaned):
        cleaned = f"{cleaned}\nLIMIT {_ROW_LIMIT}"
    return cleaned


def _generate_sql(question: str) -> str:
    specs = _load_specs()
    resp = _openai_client.chat.completions.create(
        model=_SQL_MODEL,
        messages=_build_sql_prompt(question, specs),
        temperature=0,
        max_tokens=600,
    )
    return resp.choices[0].message.content or ""


def _summarise(question: str, sql: str, rows: list[dict]) -> str:
    if not rows:
        return "По запросу ничего не найдено."
    try:
        resp = _openai_client.chat.completions.create(
            model=_SQL_MODEL,
            messages=_build_summary_prompt(question, sql, rows),
            temperature=0,
            max_tokens=600,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.error("Summary LLM error: %s", exc)
        return f"Найдено {len(rows)} строк. Ошибка форматирования ответа: {exc}"


def answer(question: str) -> dict:
    """
    End-to-end: question → answer.

    Returns: {"sql": str, "rows": list, "answer": str, "error": str | None}
    """
    question = (question or "").strip()
    if not question:
        return {"sql": "", "rows": [], "answer": "", "error": "empty question"}

    try:
        raw_sql = _generate_sql(question)
        sql = _ensure_safe_select(raw_sql)
    except Exception as exc:
        log.error("SQL generation failed: %s", exc)
        return {"sql": "", "rows": [], "answer": "", "error": f"SQL generation: {exc}"}

    try:
        rows = payouts_agent.run_sql(sql, query_limit=_ROW_LIMIT)
    except Exception as exc:
        log.error("SQL execution failed: %s", exc)
        return {"sql": sql, "rows": [], "answer": "", "error": f"SQL execution: {exc}"}

    summary = _summarise(question, sql, rows)
    return {"sql": sql, "rows": rows, "answer": summary, "error": None}
