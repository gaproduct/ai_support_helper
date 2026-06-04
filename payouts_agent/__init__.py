"""Payouts agent — Superset SQL gateway for support_tickets.

High-level Python API used by the rest of the project to answer questions
about specific payouts (and other analytics tables) by running SQL through
Superset SQL Lab.

Typical use:

    from payouts_agent import get_payout, run_sql

    info = get_payout(615175)              # one row by id
    rows = run_sql("SELECT count(*) FROM mv.t_payout_extended WHERE status='error'")

For ad-hoc CLI access:

    python -X utf8 -m support_tickets.payouts_agent.cli run-sql --show-sql \
        --sql "SELECT id, status FROM mv.t_payout_extended WHERE id = 615175"
"""

from __future__ import annotations

import os
from typing import Any

from .superset_client import SupersetClient, build_client_from_env

DEFAULT_DATABASE_ID = 11
DEFAULT_SCHEMA = "mv"
PAYOUTS_TABLE = "t_payout_extended"


def _resolve_database_id(database_id: int | None) -> int:
    if database_id is not None:
        return database_id
    env_value = os.environ.get("SUPERSET_DATABASE_ID", "").strip()
    return int(env_value) if env_value else DEFAULT_DATABASE_ID


def _resolve_schema(schema: str | None) -> str:
    if schema:
        return schema
    return os.environ.get("SUPERSET_SCHEMA", "").strip() or DEFAULT_SCHEMA


def run_sql(
    sql: str,
    database_id: int | None = None,
    schema: str | None = None,
    query_limit: int = 1000,
    max_wait_seconds: int = 120,
) -> list[dict[str, Any]]:
    """Execute SQL via Superset and return rows as a list of dicts."""
    client = build_client_from_env()
    client.authenticate()
    result = client.execute_sql(
        sql=sql,
        database_id=_resolve_database_id(database_id),
        schema=_resolve_schema(schema),
        query_limit=query_limit,
        max_wait_seconds=max_wait_seconds,
    )
    rows = result.get("rows")
    return rows if isinstance(rows, list) else []


def get_payout(payout_id: int) -> dict[str, Any] | None:
    """Fetch a single payout from t_payout_extended by id."""
    sql = (
        f"SELECT * FROM {PAYOUTS_TABLE} "
        f"WHERE id = {int(payout_id)} "
        f"LIMIT 1"
    )
    rows = run_sql(sql)
    return rows[0] if rows else None


__all__ = [
    "SupersetClient",
    "build_client_from_env",
    "run_sql",
    "get_payout",
    "DEFAULT_DATABASE_ID",
    "DEFAULT_SCHEMA",
    "PAYOUTS_TABLE",
]
