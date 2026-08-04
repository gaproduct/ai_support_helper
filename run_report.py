"""
Report runner — гарантирует дедупликацию перед формированием отчёта.

Перед генерацией любого отчёта прогоняет compute_tickets.compute() (пересчёт
таблицы tickets с кросс-кабинетным дедупом Flomni-близнецов), затем выполняет
скрипт отчёта. Отчёт печатает HTML в stdout; логи дедупа идут в stderr и не
попадают в HTML.

Запуск (из контейнера):
  docker exec -i support_tickets-scheduler-1 \
      python run_report.py --date-from 2026-07-01 --date-to 2026-07-31 \
      --report build_july_report.py > out.html
"""

import argparse
import logging
import runpy
import sys

import compute_tickets


log = logging.getLogger(__name__)


def main() -> None:
    # Логи в stderr, чтобы не смешивались с HTML отчёта в stdout.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        description="Пересчитать tickets с дедупом, затем построить отчёт."
    )
    parser.add_argument("--date-from", default=None, help="YYYY-MM-DD (диапазон пересчёта tickets)")
    parser.add_argument("--date-to", default=None, help="YYYY-MM-DD")
    parser.add_argument("--report", required=True, help="Путь к скрипту отчёта (напр. build_july_report.py)")
    parser.add_argument("--methodology", default=compute_tickets.METHODOLOGY)
    args = parser.parse_args()

    log.info(
        "Дедуп + пересчёт tickets перед отчётом (from=%s to=%s)…",
        args.date_from, args.date_to,
    )
    n = compute_tickets.compute(
        date_from=args.date_from,
        date_to=args.date_to,
        methodology=args.methodology,
    )
    log.info("tickets пересчитаны: %d записей. Формирую отчёт %s", n, args.report)

    # Скрипт отчёта печатает HTML в stdout.
    runpy.run_path(args.report, run_name="__main__")


if __name__ == "__main__":
    main()
