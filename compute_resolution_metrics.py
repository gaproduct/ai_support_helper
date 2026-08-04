"""
Прогон метрик времени обработки тикетов (resolution_metrics) по диапазону дат
и запись результатов в таблицу ticket_resolution_metrics.

По каждому диалогу resolution_metrics.analyze режет переписку на инциденты и
считает по каждому: first_response, resolution (кал./раб.), handoff по отделам,
статус. Одна строка таблицы = один инцидент. Апсертится по (dialog_id,
incident_index), поэтому повторный прогон идемпотентен.

CLI:
  docker exec -i support_tickets-scheduler-1 \
    python compute_resolution_metrics.py --date-from 2026-07-16 --date-to 2026-07-27
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from sqlalchemy import text

from database import engine
import resolution_metrics as rm

log = logging.getLogger(__name__)


def _isots(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def compute(date_from: str, date_to: str) -> tuple[int, int]:
    """Считает метрики по диалогам в [date_from, date_to] и апсертит их.

    Возвращает (обработано_диалогов, записано_инцидентов).
    """
    with engine.connect() as conn:
        # База — ТОЛЬКО отчётные тикеты: диалоги, у которых есть строка в tickets.
        # Множество диалогов одинаково для любой методологии (D/E меняет лишь
        # count, а не состав), поэтому фильтруем через EXISTS без привязки к
        # конкретной методологии — так метрики совпадают с отчётным множеством и
        # для июня (D), и для июля (E), и не задваиваются при наличии обеих.
        rows = conn.execute(
            text(
                """
                SELECT d.id, d.client_id, d.source, d.dialog_date, d.company,
                       d.messages_json
                FROM dialogs d
                WHERE d.messages_json IS NOT NULL
                  AND d.messages_json <> '' AND d.messages_json <> '[]'
                  AND EXISTS (
                      SELECT 1 FROM tickets t
                      WHERE t.dialog_id = d.id
                        AND t.dialog_date >= :df AND t.dialog_date <= :dt
                  )
                ORDER BY d.id
                """
            ),
            {"df": date_from, "dt": date_to},
        ).fetchall()

    n_dialogs = 0
    n_incidents = 0
    upsert = text(
        """
        INSERT INTO ticket_resolution_metrics (
            dialog_id, incident_index, client_id, source, dialog_date, company,
            started_at, first_response_at, resolved_at,
            first_response_seconds, resolution_seconds, resolution_working_seconds,
            handoff_seconds_total, handoff_by_dept, status, n_messages, computed_at
        ) VALUES (
            :dialog_id, :incident_index, :client_id, :source, :dialog_date, :company,
            :started_at, :first_response_at, :resolved_at,
            :first_response_seconds, :resolution_seconds, :resolution_working_seconds,
            :handoff_seconds_total, :handoff_by_dept, :status, :n_messages, now()
        )
        ON CONFLICT (dialog_id, incident_index) DO UPDATE SET
            client_id = EXCLUDED.client_id,
            source = EXCLUDED.source,
            dialog_date = EXCLUDED.dialog_date,
            company = EXCLUDED.company,
            started_at = EXCLUDED.started_at,
            first_response_at = EXCLUDED.first_response_at,
            resolved_at = EXCLUDED.resolved_at,
            first_response_seconds = EXCLUDED.first_response_seconds,
            resolution_seconds = EXCLUDED.resolution_seconds,
            resolution_working_seconds = EXCLUDED.resolution_working_seconds,
            handoff_seconds_total = EXCLUDED.handoff_seconds_total,
            handoff_by_dept = EXCLUDED.handoff_by_dept,
            status = EXCLUDED.status,
            n_messages = EXCLUDED.n_messages,
            computed_at = now()
        """
    )

    with engine.begin() as conn:
        for r in rows:
            try:
                raw = json.loads(r.messages_json)
            except (ValueError, TypeError):
                log.warning("dialog %s: невалидный messages_json, пропуск", r.id)
                continue
            metrics = rm.analyze(raw)
            if not metrics:
                continue
            n_dialogs += 1
            for m in metrics:
                conn.execute(
                    upsert,
                    {
                        "dialog_id": r.id,
                        "incident_index": m.incident_index,
                        "client_id": r.client_id,
                        "source": r.source,
                        "dialog_date": r.dialog_date,
                        "company": r.company,
                        "started_at": _isots(m.started_at),
                        "first_response_at": _isots(m.first_response_at),
                        "resolved_at": _isots(m.resolved_at),
                        "first_response_seconds": m.first_response_seconds,
                        "resolution_seconds": m.resolution_seconds,
                        "resolution_working_seconds": m.resolution_working_seconds,
                        "handoff_seconds_total": m.handoff_seconds_total,
                        "handoff_by_dept": json.dumps(m.handoff_seconds_by_dept, ensure_ascii=False),
                        "status": m.status,
                        "n_messages": m.n_messages,
                    },
                )
                n_incidents += 1
    return n_dialogs, n_incidents


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    ap = argparse.ArgumentParser(description="Прогон resolution-метрик и запись в БД")
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    args = ap.parse_args()

    nd, ni = compute(args.date_from, args.date_to)
    log.info("готово: диалогов=%d, инцидентов записано=%d", nd, ni)


if __name__ == "__main__":
    main()
