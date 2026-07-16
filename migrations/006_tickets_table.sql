-- Migration 006: tickets table
-- Хранит рассчитанное количество тикетов на диалог по методологии D.
-- Пересчитывается джобом compute_tickets.py при изменении правил или данных.

CREATE TABLE IF NOT EXISTS tickets (
    id              SERIAL PRIMARY KEY,
    dialog_id       INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
    methodology     VARCHAR(4) NOT NULL DEFAULT 'D',
    count           SMALLINT NOT NULL DEFAULT 1,
    side            VARCHAR(16),                    -- customer | executor
    category        VARCHAR(255),
    company         VARCHAR(512),
    dialog_date     DATE,
    source          VARCHAR(32),
    executor_emails JSONB DEFAULT '[]',             -- список email исполнителей (для аудита)
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dialog_id, methodology)
);

CREATE INDEX IF NOT EXISTS idx_tickets_dialog_date ON tickets (dialog_date);
CREATE INDEX IF NOT EXISTS idx_tickets_company     ON tickets (company);
CREATE INDEX IF NOT EXISTS idx_tickets_category    ON tickets (category);
CREATE INDEX IF NOT EXISTS idx_tickets_side        ON tickets (side);
