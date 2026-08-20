-- Add client_email column to incoming_messages.
--
-- Background: Flomni отдаёт контакты клиента в поле metaData, а не в profile.
-- У виджета на сайте и у ЛК заказчика profile приходит пустым, зато в metaData
-- лежат «Email» и «Имя клиента». Раньше вебхук читал только profile.name,
-- поэтому email клиента терялся.
--
-- Этот email — прямой путь к компании: по домену (ragradus.ru → ООО «Градус»)
-- или через Superset, без ожидания, пока адрес всплывёт в тексте переписки.

BEGIN;

ALTER TABLE incoming_messages
  ADD COLUMN IF NOT EXISTS client_email varchar(320);

COMMIT;
