-- Company aliases — таблица синонимов для унификации поля company.
--
-- Назначение:
--   Маппит «грязные» варианты названия компании в одно каноническое имя.
--   Применяется как шаг 4 пайплайна company_attribution (после Superset/group_name).
--
-- Пример:
--   alias = 'ООО ЭКТИВЕЙТ'        -> canonical = 'Эктивейт'
--   alias = 'Эктивейт ООО'        -> canonical = 'Эктивейт'
--   alias = 'ООО "Эктивейт"'      -> canonical = 'Эктивейт'
--
-- Заполняется руками или через детектор дублей (см. tools/detect_company_dupes.py).

BEGIN;

CREATE TABLE IF NOT EXISTS company_aliases (
    alias       text PRIMARY KEY,
    canonical   text NOT NULL,
    note        text,
    created_at  timestamp without time zone DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_company_aliases_canonical
    ON company_aliases (canonical);

COMMIT;
