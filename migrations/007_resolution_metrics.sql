-- Migration 007: ticket_resolution_metrics table
-- Хранит рассчитанные метрики времени обработки тикета по каждому ИНЦИДЕНТУ
-- диалога (resolution_metrics.analyze режет диалог на инциденты по паузе).
-- Пересчитывается джобом compute_resolution_metrics.py.

CREATE TABLE IF NOT EXISTS ticket_resolution_metrics (
    id                          SERIAL PRIMARY KEY,
    dialog_id                   INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
    incident_index              SMALLINT NOT NULL DEFAULT 0,
    client_id                   VARCHAR(512),
    source                      VARCHAR(32),
    dialog_date                 DATE,
    company                     VARCHAR(512),
    -- временные точки инцидента
    started_at                  TIMESTAMPTZ,
    first_response_at           TIMESTAMPTZ,
    resolved_at                 TIMESTAMPTZ,
    -- длительности (секунды)
    first_response_seconds      DOUBLE PRECISION,
    resolution_seconds          DOUBLE PRECISION,
    resolution_working_seconds  DOUBLE PRECISION,
    handoff_seconds_total       DOUBLE PRECISION,
    handoff_by_dept             JSONB DEFAULT '{}',
    -- прочее
    status                      VARCHAR(32),
    n_messages                  INTEGER,
    computed_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dialog_id, incident_index)
);

CREATE INDEX IF NOT EXISTS idx_trm_dialog_date ON ticket_resolution_metrics (dialog_date);
CREATE INDEX IF NOT EXISTS idx_trm_status      ON ticket_resolution_metrics (status);
CREATE INDEX IF NOT EXISTS idx_trm_company     ON ticket_resolution_metrics (company);
