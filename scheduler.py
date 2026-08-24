"""
Scheduler — runs periodic jobs for the Flomni pipeline.

Jobs:
  - flomni_history      : every 24 hours at 02:00 UTC  (Get MessageHistory — Flomni)
  - chatapp_history     : every 24 hours at 02:30 UTC  (daily incremental for ChatApp,
                          окно last 25h, идемпотентно по (chat × day))
  - ai_analysis         : every 24 hours at 04:00 UTC  (унифицированная
                          классификация по методологии май-отчёта: side +
                          category, «Потенциальный клиент» — одно из значений)
  - dialog_index        : every 24 hours at 04:45 UTC  (векторы и выжимки
                          «чем закончилось» для кабинета оператора, сразу после
                          ai_analysis: индексируются только разобранные диалоги)
  - company_attribution : every 24 hours at 05:00 UTC  (email extract → Superset resolve
                          → group-name override)
  - compute_metrics     : every 24 hours at 06:00 UTC  (tickets → fine subcategory →
                          resolution metrics, скользящее окно последних дней)

The FastAPI webhook server (webhook_flomni.py) runs as a separate process
via uvicorn (see docker-compose.yml).
"""

import logging
from datetime import date, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import ai_analysis
import chatapp_history
import company_attribution
import compute_fine_subcategory
import compute_resolution_metrics
import compute_tickets
import dialog_index
import flomni_history
from config import settings
from database import create_tables


logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


# On a sleep-prone local host (Docker on macOS) the process clock stops while
# the machine sleeps, so cron fires are missed by many hours. A 24h grace +
# coalesce lets each daily job still run once when the host wakes, instead of
# APScheduler silently skipping it.
_MISFIRE_GRACE = 24 * 3600

# Диалоги догружаются и переразмечаются задним числом, поэтому пересчитываем не
# только вчерашний день, а окно. Все три шага идемпотентны (UPSERT по dialog_id).
_METRICS_WINDOW_DAYS = 7


def compute_metrics() -> None:
    """Финальные стадии пайплайна: тикеты, подкатегории, метрики решения."""
    hi = date.today()
    lo = hi - timedelta(days=_METRICS_WINDOW_DAYS)
    lo_s, hi_s = lo.isoformat(), hi.isoformat()
    log.info("compute_metrics: окно %s..%s", lo_s, hi_s)

    n_tickets = compute_tickets.compute(date_from=lo_s, date_to=hi_s)
    log.info("compute_metrics: тикетов записано=%d", n_tickets)

    n_sub = compute_fine_subcategory.compute(lo_s, hi_s)
    log.info("compute_metrics: подкатегорий записано=%d", n_sub)

    n_dialogs, n_incidents = compute_resolution_metrics.compute(lo_s, hi_s)
    log.info(
        "compute_metrics: диалогов=%d, инцидентов=%d", n_dialogs, n_incidents
    )


def main() -> None:
    create_tables()
    log.info("Database ready.")

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        flomni_history.run,
        trigger=CronTrigger(hour=2, minute=0),
        id="flomni_history",
        name="Fetch Flomni message history",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    scheduler.add_job(
        chatapp_history.run_daily,
        trigger=CronTrigger(hour=2, minute=30),
        id="chatapp_history",
        name="ChatApp daily history backfill (last 25h)",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    scheduler.add_job(
        ai_analysis.run,
        trigger=CronTrigger(hour=4, minute=0),
        id="ai_analysis",
        name="AI analysis of dialogs",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    scheduler.add_job(
        dialog_index.run,
        trigger=CronTrigger(hour=4, minute=45),
        id="dialog_index",
        name="Dialog index for the operator cabinet (embeddings + outcomes)",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    scheduler.add_job(
        company_attribution.run,
        trigger=CronTrigger(hour=5, minute=0),
        id="company_attribution",
        name="Daily company attribution (email→Superset→group-name)",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    scheduler.add_job(
        compute_metrics,
        trigger=CronTrigger(hour=6, minute=0),
        id="compute_metrics",
        name="Tickets + fine subcategory + resolution metrics",
        misfire_grace_time=_MISFIRE_GRACE,
        coalesce=True,
    )

    log.info("Scheduler starting. Jobs: %s", [j.name for j in scheduler.get_jobs()])

    # Recover from any downtime before entering the blocking scheduler loop.
    flomni_history.detect_gap_and_backfill()

    scheduler.start()


if __name__ == "__main__":
    main()
