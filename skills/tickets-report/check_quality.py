"""Проверка качества разметки перед сборкой отчёта.

Правило: не должно быть тикета без категории, всему нужно давать специфику.
Скрипт показывает:
  1. Тикеты по дням (есть ли данные за весь период).
  2. Тикеты без категории (должно быть 0).
  3. Распределение по эффективным категориям.
  4. Тикеты без подкатегории в сегментируемых категориях, с summary,
     чтобы их можно было разметить через apply_manual_subcategory.py.
  5. Тикеты чатов-провайдеров и внутренних чатов (должно быть 0).

Период: переменные окружения REPORT_FROM / REPORT_TO (YYYY-MM-DD).

Запуск:
  ssh agusev@gcp-eu-west1b-llm.madetask.tech \
    "docker exec -i -e REPORT_FROM=2026-10-01 -e REPORT_TO=2026-10-08 \
     support_tickets-webhook-1 python -" < check_quality.py
"""
import os

from sqlalchemy import text
from database import engine
import fine_subcategory as fs

DATE_FROM = os.environ.get("REPORT_FROM", "")
DATE_TO = os.environ.get("REPORT_TO", "")
if not DATE_FROM or not DATE_TO:
    raise SystemExit("Задайте REPORT_FROM и REPORT_TO (YYYY-MM-DD)")

# Сегментируемые категории: в отчёте у них обязательна подкатегория
SUBCAT_CATEGORIES = (
    "Выплаты и проблемы с ними",
    "KYC",
    "Запрос документов",
    "Вопросы по работе в сервисе",
    "Техническая проблема/вопрос",
)

P = {"a": DATE_FROM, "b": DATE_TO, "depbucket": fs.DEPOSIT_BUCKET,
     "depcat": fs.DEPOSIT_CATEGORY}

with engine.connect() as c:
    print("1. Тикеты по дням:")
    for r in c.execute(text("""
        SELECT t.dialog_date::date d, count(*) rows, sum(t.count) tickets
        FROM tickets t
        WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
        GROUP BY 1 ORDER BY 1
    """), P):
        print("  ", r[0], "строк:", r[1], "тикетов:", r[2])

    print("\n2. Тикеты без категории (должно быть пусто):")
    rows = list(c.execute(text("""
        SELECT t.dialog_id, t.dialog_date::date, t.side, t.company
        FROM tickets t
        WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
          AND (t.category IS NULL OR t.category = '')
        ORDER BY t.dialog_id
    """), P))
    print("   count:", len(rows))
    for r in rows:
        print("  ", list(r))

    print("\n3. Эффективные категории:")
    for r in c.execute(text("""
        SELECT COALESCE(ovr.category, dep.cat, t.category) eff, sum(t.count)
        FROM tickets t
        LEFT JOIN manual_category_override ovr ON ovr.dialog_id = t.dialog_id
        LEFT JOIN (SELECT dialog_id, :depcat AS cat FROM dialog_fine_subcategory
                   WHERE fine_bucket = :depbucket) dep ON dep.dialog_id = t.dialog_id
        WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
        GROUP BY 1 ORDER BY 2 DESC
    """), P):
        print("  ", r[0], r[1])

    print("\n4. Без подкатегории в сегментируемых категориях:")
    rows = list(c.execute(text("""
        SELECT t.dialog_id, t.dialog_date::date, t.side, t.company, t.category,
               ar.summary
        FROM tickets t
        JOIN (SELECT dialog_id, MAX(id) mx FROM analysis_results GROUP BY dialog_id) lx
             ON lx.dialog_id = t.dialog_id
        JOIN analysis_results ar ON ar.id = lx.mx
        LEFT JOIN manual_subcategory_override mso ON mso.dialog_id = t.dialog_id
        LEFT JOIN manual_category_override ovr ON ovr.dialog_id = t.dialog_id
        LEFT JOIN dialog_fine_subcategory dep ON dep.dialog_id = t.dialog_id
             AND dep.fine_bucket = :depbucket
        WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
          AND t.category IN ('Выплаты и проблемы с ними','KYC','Запрос документов',
                             'Вопросы по работе в сервисе','Техническая проблема/вопрос')
          AND COALESCE(ovr.subcategory, NULLIF(ar.subcategory,''), mso.subcategory) IS NULL
          AND ovr.category IS NULL
          AND dep.dialog_id IS NULL
        ORDER BY t.dialog_id
    """), P))
    print("   count:", len(rows), "(должно быть 0)")
    for r in rows:
        s = (r[5] or "").replace("\n", " ")[:160]
        print("  ", r[0], r[1], r[2], r[3], r[4], "|", s)

    print("\n5. Тикеты провайдерских/внутренних чатов (должно быть пусто):")
    rows = list(c.execute(text("""
        SELECT t.dialog_id, t.dialog_date::date, t.company, d.chat_name
        FROM tickets t JOIN dialogs d ON d.id = t.dialog_id
        WHERE t.methodology='E' AND t.dialog_date BETWEEN :a AND :b
          AND (d.client_id::text LIKE '25ee7201%'
               OR d.client_id::text LIKE 'ed3c0fcd%'
               OR d.client_id::text LIKE 'fc1e4ebf%')
        ORDER BY t.dialog_id
    """), P))
    print("   count:", len(rows))
    for r in rows:
        print("  ", list(r))
