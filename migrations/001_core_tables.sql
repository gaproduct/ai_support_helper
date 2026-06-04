-- Core operational tables — live pipeline.
-- Эти таблицы также автоматически создаются SQLAlchemy через database.Base.metadata
-- (см. database.create_tables()), но миграция нужна для bootstrap без приложения
-- (psql -f) и для воспроизведения схемы 1-в-1.
--
-- Содержит:
--   incoming_messages — очередь входящих сообщений (Flomni / ChatApp)
--   dialogs           — единая история диалогов всех источников
--   analysis_results  — Claude AI-разбор (1 строка на dialog)
--   scenario_drafts   — связка тредов сценариев с draft-каналом Slack

BEGIN;

-- ───────────────────────── incoming_messages ─────────────────────────

CREATE TABLE IF NOT EXISTS incoming_messages (
    id                   serial PRIMARY KEY,
    client_id            varchar(255) NOT NULL,
    name                 varchar(512),
    first_message_text   text,
    first_message_at     varchar(64),
    last_message_at      varchar(64),
    done                 boolean NOT NULL DEFAULT false,
    auto_response_sent   boolean NOT NULL DEFAULT false,
    slack_thread_ts      varchar(64),
    pending_scenario     varchar(64),
    pending_data         text,
    source               varchar(32) NOT NULL DEFAULT 'flomni',
    license_id           varchar(64),
    messenger_type       varchar(32),
    created_at           timestamp without time zone DEFAULT now(),
    updated_at           timestamp without time zone DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_incoming_messages_client_id ON incoming_messages (client_id);
CREATE INDEX IF NOT EXISTS ix_incoming_messages_done      ON incoming_messages (done);
CREATE INDEX IF NOT EXISTS ix_incoming_messages_source    ON incoming_messages (source);

-- ───────────────────────── dialogs ─────────────────────────

CREATE TABLE IF NOT EXISTS dialogs (
    id              serial PRIMARY KEY,
    client_id       varchar(512) NOT NULL,
    source          varchar(32)  NOT NULL,         -- flomni | telegram | gmail | chatapp | bitrix
    messages_text   text         DEFAULT '',
    messages_json   text,                          -- JSON-массив сообщений (ChatApp)
    started_at      varchar(64),
    finished_at     varchar(64),
    processed       boolean      NOT NULL DEFAULT false,
    created_at      timestamp without time zone DEFAULT now(),
    updated_at      timestamp without time zone DEFAULT now(),
    -- ChatApp-only
    license_id      varchar(64),
    messenger_type  varchar(32),
    dialog_date     date,
    chat_name       varchar(512),
    phone           varchar(64),
    email           varchar(255),
    messages_count  integer,
    executor_email  varchar(255),
    company         varchar(512)
);

CREATE INDEX IF NOT EXISTS ix_dialogs_client_id      ON dialogs (client_id);
CREATE INDEX IF NOT EXISTS ix_dialogs_processed      ON dialogs (processed);
CREATE INDEX IF NOT EXISTS ix_dialogs_dialog_date    ON dialogs (dialog_date);
CREATE INDEX IF NOT EXISTS ix_dialogs_executor_email ON dialogs (executor_email);
CREATE INDEX IF NOT EXISTS ix_dialogs_company        ON dialogs (company);

-- Идемпотентность ChatApp: один диалог = (license, messenger, chat, day)
CREATE UNIQUE INDEX IF NOT EXISTS uq_dialogs_chatapp_chat_day
    ON dialogs (license_id, messenger_type, client_id, dialog_date)
    WHERE source = 'chatapp';

-- ───────────────────────── analysis_results ─────────────────────────

CREATE TABLE IF NOT EXISTS analysis_results (
    id            serial PRIMARY KEY,
    dialog_id     integer NOT NULL REFERENCES dialogs(id),
    summary       text,
    category      varchar(256),
    subcategory   varchar(256),
    sentiment     varchar(32),
    priority      varchar(32),
    resolution    varchar(32),
    raw_response  text,
    created_at    timestamp without time zone DEFAULT now()
);

-- ───────────────────────── scenario_drafts ─────────────────────────

CREATE TABLE IF NOT EXISTS scenario_drafts (
    id                   serial PRIMARY KEY,
    incoming_message_id  integer NOT NULL REFERENCES incoming_messages(id),
    scenario_name        varchar(64) NOT NULL,
    original_channel     varchar(64),
    original_thread_ts   varchar(64),
    draft_channel        varchar(64),
    draft_thread_ts      varchar(64),
    notification_text    text,
    active_draft_msg_ts  varchar(64),
    mirror_channel       varchar(64),
    mirror_msg_ts        varchar(64),
    created_at           timestamp without time zone DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_scenario_drafts_incoming_message_id ON scenario_drafts (incoming_message_id);
CREATE INDEX IF NOT EXISTS ix_scenario_drafts_draft_channel       ON scenario_drafts (draft_channel);
CREATE INDEX IF NOT EXISTS ix_scenario_drafts_draft_thread_ts     ON scenario_drafts (draft_thread_ts);

COMMIT;
