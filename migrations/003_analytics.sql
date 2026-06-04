-- Analytics layer — таблицы для AI-классификации и финальной аналитики.
--
-- Содержит:
--   flomni_archive_analysis    — Claude-разбор Flomni-диалогов из архива (Jan-Apr)
--   may_dialog_classification  — классификация мая 2026 по новой методологии
--   analytics_archive_jan_may  — финальная единая таблица за Jan-May 2026
--                                (5 948 строк, 1 строка = 1 тикет по новой методологии).
--                                Это основная таблица, передаваемая аналитику.
--
-- Подробное описание полей analytics_archive_jan_may — см. exports/README.md.

BEGIN;

-- ───────────────────────── flomni_archive_analysis ─────────────────────────

CREATE TABLE IF NOT EXISTS flomni_archive_analysis (
    id            serial PRIMARY KEY,
    dialog_id     integer NOT NULL UNIQUE
                  REFERENCES dialogs(id) ON DELETE CASCADE,
    summary       text,
    category      varchar(64),
    subcategory   varchar(128),
    sentiment     varchar(16),
    priority      varchar(16),
    resolution    varchar(16),
    model         varchar(64),
    raw_response  text,
    error         text,
    created_at    timestamp without time zone DEFAULT now()
);

-- ───────────────────────── may_dialog_classification ─────────────────────────

CREATE TABLE IF NOT EXISTS may_dialog_classification (
    id            serial PRIMARY KEY,
    dialog_id     integer NOT NULL UNIQUE
                  REFERENCES dialogs(id) ON DELETE CASCADE,
    channel       varchar(16),     -- mt_flomni | remozo
    side          varchar(16),     -- executor | customer | other
    category      varchar(128),
    rationale     text,
    model         varchar(64),
    raw_response  text,
    error         text,
    fragment_of   integer,         -- ссылка на родительский dialog (если фрагмент)
    created_at    timestamp without time zone DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_may_cls_channel  ON may_dialog_classification (channel);
CREATE INDEX IF NOT EXISTS ix_may_cls_side     ON may_dialog_classification (side);
CREATE INDEX IF NOT EXISTS ix_may_cls_category ON may_dialog_classification (category);

-- ───────────────────────── analytics_archive_jan_may ─────────────────────────

CREATE TABLE IF NOT EXISTS analytics_archive_jan_may (
    id              serial PRIMARY KEY,
    -- Identification
    source_table    varchar(32),                  -- unified | live
    source_id       integer,
    source          varchar(16),                  -- chatapp | bitrix | flomni
    -- Timestamps
    dialog_date     date,
    started_at      varchar(64),
    finished_at     varchar(64),
    -- Channel / chat
    license_id      varchar(64),
    messenger_type  varchar(32),
    chat_id         varchar(255),
    chat_name       varchar(512),
    chat_url        text,
    -- Attribution
    executor_email  varchar(255),
    company         varchar(512),
    is_client       boolean,
    -- Content
    messages_count  integer,
    messages_text   text,                         -- JSON-массив сообщений
    -- Legacy AI classification (Jan-Apr)
    ai_summary      text,
    ai_category     varchar(64),
    ai_subcategory  varchar(128),
    ai_sentiment    varchar(16),
    ai_priority     varchar(16),
    ai_resolution   varchar(16),
    -- Unified May classification
    may_channel     varchar(16),                  -- chatapp_legacy | bitrix_legacy | mt_flomni | remozo | flomni_legacy
    may_side        varchar(16),                  -- customer | executor
    may_category    varchar(128),
    may_rationale   text
);

CREATE INDEX IF NOT EXISTS ix_aajm_date    ON analytics_archive_jan_may (dialog_date);
CREATE INDEX IF NOT EXISTS ix_aajm_source  ON analytics_archive_jan_may (source);
CREATE INDEX IF NOT EXISTS ix_aajm_company ON analytics_archive_jan_may (company);
CREATE INDEX IF NOT EXISTS ix_aajm_may_cat ON analytics_archive_jan_may (may_category);
CREATE INDEX IF NOT EXISTS ix_aajm_ai_cat  ON analytics_archive_jan_may (ai_category);

COMMIT;
