# Migrations — быстрая развертка БД

Идемпотентные SQL-миграции для создания схемы PostgreSQL с нуля.
Запускаются строго по порядку имени файла (нумерация `NNN_`).

| # | Файл | Что создаёт |
|---|---|---|
| 1 | `001_core_tables.sql` | Операционные таблицы (live pipeline): `incoming_messages`, `dialogs`, `analysis_results`, `scenario_drafts` |
| 2 | `002_raw_messages.sql` | Raw-хранилище сырых сообщений: `bitrix_messages_raw`, `chatapp_messages_raw` |
| 3 | `003_analytics.sql` | Аналитика и AI-классификация: `flomni_archive_analysis`, `may_dialog_classification`, `analytics_archive_jan_may` |
| 4 | `004_company_aliases.sql` | Таблица синонимов компаний `company_aliases` (для step 4 атрибуции) |

Все скрипты используют `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` —
повторный запуск безопасен.

## Развертка на новом сервере

### Вариант 1 — Docker Compose (рекомендуется)

```bash
cp .env.example .env          # заполнить креды и API-ключи
docker compose up -d db       # поднять PostgreSQL 16

# Применить миграции (по порядку):
for f in migrations/*.sql; do
    docker compose exec -T db psql -U postgres -d support_tickets -f "/migrations/$(basename $f)" \
      < "$f"
done

docker compose up -d          # запустить scheduler + webhook server
```

### Вариант 2 — bare-metal PostgreSQL

```bash
createdb -U postgres support_tickets

for f in migrations/*.sql; do
    psql -U postgres -d support_tickets -f "$f"
done
```

### Вариант 3 — через SQLAlchemy (только core-таблицы)

`scheduler.py` и `main.py` при старте вызывают `database.create_tables()`,
который автоматически создаёт таблицы из `001_core_tables.sql` через ORM.
Для `002_*` и `003_*` SQL-миграции запустить вручную.

## Загрузка данных аналитики

Готовый дамп `analytics_archive_jan_may` (5948 строк, схема + данные) лежит
в `exports/analytics_archive_jan_may.sql`. После применения `003_analytics.sql`
дамп можно подгрузить:

```bash
psql -U postgres -d support_tickets -f exports/analytics_archive_jan_may.sql
```

Описание полей и сэмплы — `exports/README.md`.

## Сидинг справочников

После миграций нужно залить справочные данные из `seed/`:

```bash
docker compose exec -T db psql -U postgres -d support_tickets \
    < seed/company_aliases.sql
```

| Файл | Что заливает |
|---|---|
| `seed/company_aliases.sql` | Маппинг алиасов компаний → каноническое имя (используется `apply_company_aliases` как step 4 атрибуции) |

Все сиды используют `ON CONFLICT … DO UPDATE` — повторный запуск безопасен,
ручные правки в строках с тем же `alias` будут перезаписаны.

