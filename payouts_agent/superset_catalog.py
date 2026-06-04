from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ANALYTICAL_TABLES = (
    "TASKS MAIN",
    "COMPANY MAIN",
    "PAYOUTS MAIN",
    "PROFIT AND LOSS",
)

DEFAULT_METADATA_DATABASE_ID = int(os.environ.get("SUPERSET_METADATA_DATABASE_ID", "6"))
DEFAULT_METADATA_SCHEMA = os.environ.get("SUPERSET_METADATA_SCHEMA", "public")
DEFAULT_CATALOG_CACHE_PATH = Path(__file__).resolve().parent / "session_state" / "live_table_catalog.json"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _as_bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def _normalize_table_names(table_names: Iterable[str] | None) -> list[str]:
    names = list(table_names) if table_names is not None else list(DEFAULT_ANALYTICAL_TABLES)
    return [name for name in names if name]


def load_live_table_catalog(
    client,
    table_names: Iterable[str] | None = None,
    metadata_database_id: int = DEFAULT_METADATA_DATABASE_ID,
    metadata_schema: str = DEFAULT_METADATA_SCHEMA,
) -> dict[str, Any]:
    """
    Load the current Superset dataset metadata for the analytics tables.

    The returned structure is intentionally simple and JSON-friendly so it can be
    cached, inspected by the assistant, or serialized for prompt context.
    """
    requested_tables = _normalize_table_names(table_names)
    if not requested_tables:
        raise ValueError("No table names provided for metadata loading")

    table_filter = ", ".join(_sql_literal(name) for name in requested_tables)
    sql = f"""
        SELECT
            t.id AS dataset_id,
            t.table_name,
            t.schema,
            t.catalog,
            t.database_id,
            d.database_name,
            t.main_dttm_col,
            t.is_sqllab_view,
            NOT t.sql IS NULL AND btrim(coalesce(t.sql, '')) <> '' AS has_sql_text,
            c.id AS column_id,
            c.column_name,
            c.verbose_name,
            c.groupby,
            c.filterable,
            c.is_dttm,
            c.expression,
            c.type,
            c.description
        FROM tables AS t
        JOIN table_columns AS c
            ON c.table_id = t.id
        LEFT JOIN dbs AS d
            ON d.id = t.database_id
        WHERE t.table_name IN ({table_filter})
          AND c.description IS NOT NULL
        ORDER BY
            t.table_name,
            c.id
    """

    result = client.execute_sql(
        sql=sql,
        database_id=metadata_database_id,
        schema=metadata_schema,
        query_limit=5000,
        max_wait_seconds=120,
    )
    rows = list(result.get("rows", []))

    datasets: dict[str, dict[str, Any]] = {}
    field_index: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        table_name = row["table_name"]
        dataset = datasets.setdefault(
            table_name,
            {
                "dataset_id": row["dataset_id"],
                "table_name": table_name,
                "schema": row["schema"],
                "catalog": row["catalog"],
                "database_id": row["database_id"],
                "database_name": row["database_name"],
                "main_dttm_col": row["main_dttm_col"],
                "is_sqllab_view": _as_bool(row["is_sqllab_view"]),
                "has_sql_text": _as_bool(row["has_sql_text"]),
                "columns": [],
            },
        )

        column_entry = {
            "column_id": row["column_id"],
            "column_name": row["column_name"],
            "verbose_name": row["verbose_name"],
            "groupby": _as_bool(row["groupby"]),
            "filterable": _as_bool(row["filterable"]),
            "is_dttm": _as_bool(row["is_dttm"]),
            "expression": row["expression"],
            "type": row["type"],
            "description": row["description"],
            "sql_ref": f"{table_name}.{row['column_name']}",
        }
        dataset["columns"].append(column_entry)
        field_index[row["column_name"].lower()].append(
            {
                "table_name": table_name,
                "column_name": row["column_name"],
                "description": row["description"],
                "sql_ref": column_entry["sql_ref"],
                "is_dttm": column_entry["is_dttm"],
                "type": row["type"],
            }
        )

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "metadata_database_id": metadata_database_id,
            "metadata_schema": metadata_schema,
        },
        "requested_tables": requested_tables,
        "datasets": list(datasets.values()),
        "field_index": dict(sorted(field_index.items())),
    }
    return catalog


def save_live_table_catalog(catalog: dict[str, Any], output_path: str | Path | None = None) -> Path:
    path = Path(output_path) if output_path else DEFAULT_CATALOG_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_cached_table_catalog(path: str | Path | None = None) -> dict[str, Any]:
    candidate = Path(path) if path else DEFAULT_CATALOG_CACHE_PATH
    return json.loads(candidate.read_text(encoding="utf-8"))
