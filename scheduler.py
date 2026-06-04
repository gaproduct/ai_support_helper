"""
Scheduler — runs periodic jobs for the Flomni pipeline.

Jobs:
  - flomni_history      : every 24 hours at 02:00 UTC  (Get MessageHistory — Flomni)
  - chatapp_history     : every 24 hours at 02:30 UTC  (daily incremental for ChatApp,
                          окно last 25h, идемпотентно по (chat × day))
  - ai_analysis         : every 24 hours at 04:00 UTC  (Support Ticket AI Research)
  - company_attribution : every 24 hours at 05:00 UTC  (email extract → Superset resolve
                          → group-name override)

The FastAPI webhook server (webhook_flomni.py) runs as a separate process
via uvicorn (see docker-compose.yml).
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import ai_analysis
import chatapp_history
import company_attribution
import flomni_history
from config import settings
from database import create_tables


logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def main() -> None:
    create_tables()
    log.info("Database ready.")

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        flomni_history.run,
        trigger=CronTrigger(hour=2, minute=0),
        id="flomni_history",
        name="Fetch Flomni message history",
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        chatapp_history.run_daily,
        trigger=CronTrigger(hour=2, minute=30),
        id="chatapp_history",
        name="ChatApp daily history backfill (last 25h)",
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        ai_analysis.run,
        trigger=CronTrigger(hour=4, minute=0),
        id="ai_analysis",
        name="AI analysis of dialogs",
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        company_attribution.run,
        trigger=CronTrigger(hour=5, minute=0),
        id="company_attribution",
        name="Daily company attribution (email→Superset→group-name)",
        misfire_grace_time=3600,
    )

    log.info("Scheduler starting. Jobs: %s", [j.name for j in scheduler.get_jobs()])
    scheduler.start()


if __name__ == "__main__":
    main()
