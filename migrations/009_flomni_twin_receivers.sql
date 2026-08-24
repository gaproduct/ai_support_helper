-- Реестр парных получателей Flomni.
--
-- Один и тот же TG-чат подключён к Flomni двумя каналами. Flomni шлёт два
-- вебхука с разными `receiver`, разница около 150 мс, текст сообщения
-- побайтово одинаковый. Из-за этого заводятся два треда в Slack, два диалога
-- и два тикета.
--
-- Ответы поддержки уходят только через один канал. У второго в истории всегда
-- одни входящие. Его и считаем теневым: канонический получатель тот, у кого
-- есть исходящие.
--
-- Таблица заполняется скриптом flomni_twins.py и пополняется вебхуком, когда
-- он ловит новую пару.

BEGIN;

CREATE TABLE IF NOT EXISTS flomni_twin_receivers (
    shadow_client_id     text PRIMARY KEY,
    canonical_client_id  text NOT NULL,
    shadow_name          text,
    canonical_name       text,
    -- Сколько совпавших сообщений подтвердили пару. Чем больше, тем надёжнее.
    evidence             integer NOT NULL DEFAULT 1,
    detected_by          text NOT NULL DEFAULT 'scan',  -- 'scan' | 'webhook'
    detected_at          timestamp NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_flomni_twin_canonical
    ON flomni_twin_receivers (canonical_client_id);

-- Реестр теневых дублей диалогов. Раньше создавался вручную при разборе июля,
-- в миграциях его не было. Фиксируем схему, чтобы разворачивалось с нуля.
CREATE TABLE IF NOT EXISTS shadow_duplicate_dialogs (
    dialog_id            integer PRIMARY KEY,
    client_id            text,
    source               text,
    dialog_date          date,
    canonical_dialog_id  integer,
    canonical_client_id  text,
    reason               text NOT NULL DEFAULT 'client_only_shadow',
    marked_at            timestamp NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_shadow_dupes_client
    ON shadow_duplicate_dialogs (client_id);

-- Пометка на записи входящего: это близнец, тред в Slack у него общий с
-- указанным получателем. Историю по нему всё равно тянем, иначе потеряем
-- сообщения клиента, которые второй канал не записал. А вот в Slack и в
-- автоответчик такая запись больше не идёт.
ALTER TABLE incoming_messages
  ADD COLUMN IF NOT EXISTS twin_of varchar(255);

COMMIT;
