-- Статусы тикета, проставляемые оператором из карточки обращения.
-- Один ряд = один переход. Текущий статус — последний по set_at.
--
-- Зачем: спецификация SLA написана в статусах («В работе у поддержки»,
-- «Ожидает комплаенса»...), а в данных их нет. По тексту переписки статусы
-- восстанавливаются плохо (совпадение с ручной разметкой 8 из 18), поэтому
-- ORT без этой таблицы не считается. Здесь — первоисточник.

CREATE TABLE IF NOT EXISTS ticket_status_events (
    id          SERIAL PRIMARY KEY,
    incoming_id INTEGER NOT NULL REFERENCES incoming_messages(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    set_by      TEXT NOT NULL DEFAULT '',
    set_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tse_incoming ON ticket_status_events (incoming_id, set_at);
