"""CLI entrypoint for the payouts agent.

Mirrors the original llm_ux/run_superset.py interface but uses relative imports
so it lives inside the support_tickets package.

Usage examples:
    python -X utf8 -m support_tickets.payouts_agent.cli login
    python -X utf8 -m support_tickets.payouts_agent.cli dump-catalog
    python -X utf8 -m support_tickets.payouts_agent.cli build-semantic-index
    python -X utf8 -m support_tickets.payouts_agent.cli run-sql --show-sql \
        --sql "SELECT id, status FROM mv.t_payout_extended WHERE id = 615175"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .superset_client import build_client_from_env
from .superset_catalog import (
    DEFAULT_CATALOG_CACHE_PATH,
    DEFAULT_ANALYTICAL_TABLES,
    load_live_table_catalog,
    load_cached_table_catalog,
    save_live_table_catalog,
)
from .semantic_index import (
    DEFAULT_SEMANTIC_INDEX_PATH,
    build_semantic_index,
    resolve_query_terms,
    save_semantic_index,
)


SESSION_STATE_DIR = Path(__file__).resolve().parent / "session_state"
LAST_RESULT_PATH = SESSION_STATE_DIR / "last_result.json"
LAST_QUERY_PATH = SESSION_STATE_DIR / "last_query.sql"
LAST_METADATA_PATH = SESSION_STATE_DIR / "last_metadata.json"


def load_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.sql_file:
        return Path(args.sql_file).read_text(encoding="utf-8")
    raise ValueError("Provide either --sql or --sql-file")


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def should_print_sql(cli_value: bool) -> bool:
    if cli_value:
        return True
    env_value = os.environ.get("LLM_UX_PRINT_SQL", "false").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def resolve_database_id(cli_value: int | None) -> int:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get("SUPERSET_DATABASE_ID", "").strip()
    if env_value:
        return int(env_value)
    raise ValueError("Provide --database-id or set SUPERSET_DATABASE_ID in support_tickets/.env")


def resolve_schema(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    env_value = os.environ.get("SUPERSET_SCHEMA", "").strip()
    return env_value or None


def save_last_result(
    sql: str,
    database_id: int,
    schema: str | None,
    catalog: str | None,
    query_limit: int,
    result: dict[str, object],
) -> None:
    SESSION_STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows = result.get("rows")
    if not isinstance(rows, list):
        rows = []

    LAST_QUERY_PATH.write_text(sql, encoding="utf-8")
    LAST_RESULT_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    LAST_METADATA_PATH.write_text(
        json.dumps(
            {
                "database_id": database_id,
                "schema": schema,
                "catalog": catalog,
                "query_limit": query_limit,
                "row_count": len(rows),
                "result_mode": result.get("mode"),
                "client_id": result.get("client_id"),
                "query_id": result.get("query_id"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def command_ping(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    client.authenticate()
    print_json(client.get_current_user())


def command_login(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    payload = client.authenticate_login_only()
    print_json(
        {
            "login_ok": True,
            "has_access_token": bool(payload.get("access_token")),
            "has_refresh_token": bool(payload.get("refresh_token")),
            "token_type": payload.get("token_type"),
            "response_keys": sorted(payload.keys()),
        }
    )


def command_list_databases(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    client.authenticate()
    result = client.list_databases(page_size=args.page_size)
    compact = []
    for item in result.get("result", []):
        compact.append(
            {
                "id": item.get("id"),
                "database_name": item.get("database_name"),
                "backend": item.get("backend"),
                "allow_run_async": item.get("allow_run_async"),
            }
        )
    print_json(compact)


def command_run_sql(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    client.authenticate()
    sql = load_sql(args)
    database_id = resolve_database_id(args.database_id)
    schema = resolve_schema(args.schema)
    if should_print_sql(args.show_sql):
        print("-- Executing SQL", file=sys.stderr)
        print(f"-- database_id: {database_id}", file=sys.stderr)
        print(f"-- schema: {schema or '(none)'}", file=sys.stderr)
        print(sql, file=sys.stderr)
    result = client.execute_sql(
        sql=sql,
        database_id=database_id,
        schema=schema,
        catalog=args.catalog,
        query_limit=args.query_limit,
        max_wait_seconds=args.max_wait_seconds,
    )
    save_last_result(
        sql=sql,
        database_id=database_id,
        schema=schema,
        catalog=args.catalog,
        query_limit=args.query_limit,
        result=result,
    )
    print_json(result)


def command_dump_catalog(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    client.authenticate()
    table_names = args.table_name or list(DEFAULT_ANALYTICAL_TABLES)
    catalog = load_live_table_catalog(
        client=client,
        table_names=table_names,
        metadata_database_id=args.metadata_database_id,
        metadata_schema=args.metadata_schema,
    )
    output_path = args.output or DEFAULT_CATALOG_CACHE_PATH
    saved_to = save_live_table_catalog(catalog, output_path)
    print_json(
        {
            "saved_to": str(saved_to),
            "requested_tables": table_names,
            "dataset_count": len(catalog.get("datasets", [])),
            "field_count": sum(len(items) for items in catalog.get("field_index", {}).values()),
        }
    )


def command_build_semantic_index(args: argparse.Namespace) -> None:
    try:
        catalog = load_cached_table_catalog(args.catalog_path)
    except FileNotFoundError as exc:
        missing_path = args.catalog_path or str(DEFAULT_CATALOG_CACHE_PATH)
        raise ValueError(
            f"Catalog cache not found at {missing_path}. Run `dump-catalog` first."
        ) from exc

    index = build_semantic_index(catalog, min_token_length=args.min_token_length)
    output_path = args.output or DEFAULT_SEMANTIC_INDEX_PATH
    saved_to = save_semantic_index(index, output_path)

    payload: dict[str, object] = {
        "saved_to": str(saved_to),
        "dataset_count": index.get("dataset_count", 0),
        "field_count": index.get("field_count", 0),
        "term_count": len(index.get("term_index", {})),
    }
    if args.sample_query:
        payload["sample_query"] = args.sample_query
        payload["sample_matches"] = resolve_query_terms(
            args.sample_query,
            index,
            top_k=args.sample_top_k,
            min_token_length=args.min_token_length,
        )
    print_json(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Superset SQL agent for support_tickets payouts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Authenticate and show token metadata")
    login_parser.set_defaults(func=command_login)

    ping_parser = subparsers.add_parser("ping", help="Authenticate and show current user")
    ping_parser.set_defaults(func=command_ping)

    databases_parser = subparsers.add_parser("list-databases", help="List visible Superset databases")
    databases_parser.add_argument("--page-size", type=int, default=100)
    databases_parser.set_defaults(func=command_list_databases)

    run_sql_parser = subparsers.add_parser("run-sql", help="Execute SQL through Superset SQL Lab API")
    run_sql_parser.add_argument("--database-id", type=int)
    run_sql_parser.add_argument("--schema")
    run_sql_parser.add_argument("--catalog")
    run_sql_parser.add_argument("--query-limit", type=int, default=1000)
    run_sql_parser.add_argument("--max-wait-seconds", type=int, default=120)
    run_sql_parser.add_argument("--sql")
    run_sql_parser.add_argument("--sql-file")
    run_sql_parser.add_argument("--show-sql", action="store_true")
    run_sql_parser.set_defaults(func=command_run_sql)

    catalog_parser = subparsers.add_parser(
        "dump-catalog",
        help="Load Superset table metadata and cache it locally",
    )
    catalog_parser.add_argument("--output")
    catalog_parser.add_argument("--metadata-database-id", type=int, default=6)
    catalog_parser.add_argument("--metadata-schema", default="public")
    catalog_parser.add_argument(
        "--table-name",
        action="append",
        dest="table_name",
        help="Dataset name in Superset metadata DB. Can be repeated.",
    )
    catalog_parser.set_defaults(func=command_dump_catalog)

    semantic_parser = subparsers.add_parser(
        "build-semantic-index",
        help="Build semantic field map from cached live catalog",
    )
    semantic_parser.add_argument("--catalog-path")
    semantic_parser.add_argument("--output")
    semantic_parser.add_argument("--min-token-length", type=int, default=2)
    semantic_parser.add_argument("--sample-query")
    semantic_parser.add_argument("--sample-top-k", type=int, default=12)
    semantic_parser.set_defaults(func=command_build_semantic_index)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
