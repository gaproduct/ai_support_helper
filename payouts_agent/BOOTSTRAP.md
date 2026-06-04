# Assistant Bootstrap

For a new AI assistant session, start with [`assistant_runbook.yaml`](./assistant_runbook.yaml).

Minimal operating rule:

1. Read project `.env` (Superset credentials live there).
2. Refresh the live Superset metadata catalog with `python -m payouts_agent.cli dump-catalog`.
3. Build semantic map with `python -m payouts_agent.cli build-semantic-index`.
4. Use `live_table_catalog.json` and `live_semantic_index.json` as sources of truth for fields and term mapping.
5. Generate SQL from the user's natural-language question.
6. Execute SQL only via `python -m payouts_agent.cli run-sql ... --show-sql`.
7. Return both the SQL and the business answer.
8. If no direct field is found in described data, notify user that fallback inference is used and precision may be lower.

Current known production defaults:

- `database_id=11`
- `schema=mv`
- metadata source: `PostgreSQL SUPERSET` (`database_id=6`, `schema=public`)
