from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SEMANTIC_INDEX_PATH = Path(__file__).resolve().parent / "session_state" / "live_semantic_index.json"

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[А-Я]?[а-я]+|[А-ЯЁ]+(?=[А-ЯЁ]|$)|\d+")

_STOPWORDS = {
    "and",
    "or",
    "the",
    "for",
    "with",
    "from",
    "where",
    "что",
    "как",
    "для",
    "или",
    "это",
    "при",
    "без",
    "по",
    "на",
}


def _normalize_token(token: str) -> str:
    return token.strip().lower()


def _split_column_name(name: str) -> list[str]:
    parts: list[str] = []
    for raw in name.split("_"):
        parts.extend(_CAMEL_RE.findall(raw) or [raw])
    return [_normalize_token(item) for item in parts if item]


def _extract_tokens(text: str, min_len: int) -> list[str]:
    tokens = [_normalize_token(item) for item in _TOKEN_RE.findall(text)]
    return [token for token in tokens if len(token) >= min_len and token not in _STOPWORDS]


def build_semantic_index(catalog: dict[str, Any], min_token_length: int = 2) -> dict[str, Any]:
    datasets = catalog.get("datasets", [])
    entries: list[dict[str, Any]] = []
    term_index: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for dataset in datasets:
        table_name = dataset.get("table_name")
        for column in dataset.get("columns", []):
            column_name = column.get("column_name")
            description = column.get("description") or ""
            sql_ref = column.get("sql_ref") or f"{table_name}.{column_name}"

            column_tokens = _split_column_name(column_name or "")
            description_tokens = _extract_tokens(description, min_token_length)
            all_terms = sorted(set(column_tokens + description_tokens))

            entry = {
                "sql_ref": sql_ref,
                "table_name": table_name,
                "column_name": column_name,
                "description": description,
                "type": column.get("type"),
                "is_dttm": bool(column.get("is_dttm")),
                "terms": all_terms,
            }
            entries.append(entry)

            for term in all_terms:
                term_index[term].append(
                    {
                        "sql_ref": sql_ref,
                        "table_name": table_name,
                        "column_name": column_name,
                        "is_dttm": bool(column.get("is_dttm")),
                        "type": column.get("type"),
                    }
                )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_catalog_generated_at": catalog.get("generated_at"),
        "dataset_count": len(datasets),
        "field_count": len(entries),
        "entries": entries,
        "term_index": dict(sorted(term_index.items())),
    }


def save_semantic_index(index: dict[str, Any], output_path: str | Path | None = None) -> Path:
    path = Path(output_path) if output_path else DEFAULT_SEMANTIC_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_semantic_index(path: str | Path | None = None) -> dict[str, Any]:
    candidate = Path(path) if path else DEFAULT_SEMANTIC_INDEX_PATH
    return json.loads(candidate.read_text(encoding="utf-8"))


def resolve_query_terms(query: str, index: dict[str, Any], top_k: int = 12, min_token_length: int = 2) -> list[dict[str, Any]]:
    tokens = set(_extract_tokens(query, min_token_length))
    if not tokens:
        return []

    score_by_ref: dict[str, dict[str, Any]] = {}
    for token in tokens:
        for candidate in index.get("term_index", {}).get(token, []):
            sql_ref = candidate["sql_ref"]
            item = score_by_ref.setdefault(
                sql_ref,
                {
                    "sql_ref": sql_ref,
                    "table_name": candidate["table_name"],
                    "column_name": candidate["column_name"],
                    "matched_terms": [],
                    "score": 0,
                },
            )
            item["matched_terms"].append(token)
            item["score"] += 1

    ranked = sorted(
        score_by_ref.values(),
        key=lambda row: (-row["score"], row["table_name"], row["column_name"]),
    )
    return ranked[:top_k]
