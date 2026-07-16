-- Add rationale column to analysis_results.
--
-- Background: майская методология (Jan-May archive) требовала от LLM
-- выдавать `rationale` с цитатой из переписки, обосновывающей выбор
-- side. Это якорит решение в реальном содержимом и заметно повышает
-- точность определения customer / executor на edge-кейсах.
--
-- Возвращаем это поле в текущий пайплайн.

BEGIN;

ALTER TABLE analysis_results
  ADD COLUMN IF NOT EXISTS rationale text;

COMMIT;
