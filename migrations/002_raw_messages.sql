-- Raw message storage — нормализованное "сырое" хранение всех сообщений
-- из Bitrix и ChatApp. Используется для ретроспективного пересборки
-- диалогов и для backfill'а аналитики.
--
-- Содержит:
--   bitrix_messages_raw  — все сообщения Bitrix24 OpenLines
--   chatapp_messages_raw — все сообщения ChatApp (все мессенджеры)

BEGIN;

-- ───────────────────────── bitrix_messages_raw ─────────────────────────

CREATE TABLE IF NOT EXISTS bitrix_messages_raw (
    session_id    integer NOT NULL,
    message_id    bigint  NOT NULL,
    chat_id       integer,
    sender_id     varchar(64),
    sender_role   varchar(16),                  -- client | operator | bot
    message_date  timestamp with time zone NOT NULL,
    text          text,
    text_legacy   text,
    params        jsonb,
    files         jsonb,
    raw_json      text,
    created_at    timestamp with time zone DEFAULT now(),
    PRIMARY KEY (session_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_bitrix_messages_raw_session_date
    ON bitrix_messages_raw (session_id, message_date);
CREATE INDEX IF NOT EXISTS ix_bitrix_messages_raw_chat_date
    ON bitrix_messages_raw (chat_id, message_date);
CREATE INDEX IF NOT EXISTS ix_bitrix_messages_raw_sender_role
    ON bitrix_messages_raw (sender_role, message_date);

-- ───────────────────────── chatapp_messages_raw ─────────────────────────

CREATE TABLE IF NOT EXISTS chatapp_messages_raw (
    id              bigserial PRIMARY KEY,
    license_id      varchar(64)  NOT NULL,
    messenger_type  varchar(32)  NOT NULL,
    chat_id         varchar(255) NOT NULL,
    message_id      varchar(128) NOT NULL,
    internal_id     varchar(128),
    time_unix       bigint       NOT NULL,
    side            varchar(8),                 -- inbound | outbound
    msg_type        varchar(32),
    raw_json        text         NOT NULL,
    created_at      timestamp without time zone DEFAULT now(),
    CONSTRAINT uq_chatapp_messages_raw
        UNIQUE (license_id, messenger_type, chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_chatapp_messages_raw_chat_time
    ON chatapp_messages_raw (chat_id, time_unix);
CREATE INDEX IF NOT EXISTS ix_chatapp_messages_raw_license_time
    ON chatapp_messages_raw (license_id, messenger_type, time_unix);

COMMIT;
