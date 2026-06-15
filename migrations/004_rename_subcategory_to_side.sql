-- 004_rename_subcategory_to_side.sql
--
-- ai_analysis pipeline переходит на единую классификацию по методологии
-- май-отчёта: одна категория + сторона (customer/executor). Поле
-- analysis_results.subcategory переименовано в .side, чтобы новые записи
-- хранили именно сторону, а не подкатегорию.
--
-- Существующие строки (legacy «короткая» классификация) не мигрируются;
-- их .side содержит исторические значения подкатегории и не должно
-- использоваться в аналитике. Фильтровать по created_at при необходимости.
--
-- Apply:
--   docker compose exec -T db psql -U postgres -d support_tickets -f /app/migrations/004_rename_subcategory_to_side.sql

ALTER TABLE analysis_results
    RENAME COLUMN subcategory TO side;
