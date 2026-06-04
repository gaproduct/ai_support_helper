Local JSON specs are optional fallback context.

Recommended naming (if used):
- `schema.table_name.json`
- `view_name.json` if the source is already unique in your project

Use JSON only for rules that are missing in Superset metadata descriptions.

Minimal workflow:
1. First rely on live catalog from `llm_ux/session_state/live_table_catalog.json`.
2. Add JSON spec only when business logic is not represented in metadata descriptions.
3. Keep JSON short: only missing semantic rules and examples.

These files are intended for LLM context, not for execution. Keep them concise and business-oriented.
