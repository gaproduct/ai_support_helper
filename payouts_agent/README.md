# Payouts Agent

Superset SQL gateway, встроенный в `support_tickets`. Используется когда нужно
ответить на конкретный вопрос по выплате, парсить логи или поднять любую
аналитику из ClickHouse через Superset SQL Lab.

## Что внутри

- `superset_client.py` — низкоуровневый клиент (login, execute_sql, polling)
- `superset_catalog.py` — выгрузка живой метаструктуры таблиц из Superset
- `semantic_index.py` — построение семантического индекса по полям
- `cli.py` — CLI вокруг всего этого (`login`, `run-sql`, `dump-catalog`, ...)
- `__init__.py` — высокоуровневый Python API: `get_payout()`, `run_sql()`
- `table_specs/t_payout_extended.json` — статическая спецификация таблицы выплат
  (использовать как fallback / source of truth по бизнес-смыслу полей)
- `assistant_runbook.yaml` + `BOOTSTRAP.md` — инструкция для AI-агента
- `session_state/` — кэш каталога и semantic index, последние результаты SQL

## Конфигурация

В `support_tickets/.env` должны быть Superset креды:

```
SUPERSET_URL=https://...
SUPERSET_USERNAME=...
SUPERSET_PASSWORD=...
SUPERSET_PROVIDER=db
SUPERSET_VERIFY_SSL=true
SUPERSET_TIMEOUT_SECONDS=60
SUPERSET_DATABASE_ID=11
SUPERSET_SCHEMA=mv
```

Эти переменные читаются автоматически при импорте модуля.

## Программный API

```python
from payouts_agent import get_payout, run_sql

# Точечный запрос по id выплаты
payout = get_payout(615175)

# Произвольный SQL
rows = run_sql(
    "SELECT count(*) AS c FROM t_payout_extended WHERE status = 'error'"
)
```

## CLI

```bash
# Проверить аутентификацию
python -X utf8 -m support_tickets.payouts_agent.cli login

# Обновить живой каталог (читает метаданные из Superset Postgres)
python -X utf8 -m support_tickets.payouts_agent.cli dump-catalog

# Построить semantic index по живому каталогу
python -X utf8 -m support_tickets.payouts_agent.cli build-semantic-index

# Запустить SQL
python -X utf8 -m support_tickets.payouts_agent.cli run-sql --show-sql \
    --sql "SELECT id, status, contractor_email FROM t_payout_extended WHERE id = 615175"
```

## Как пользоваться (рекомендованный flow)

1. Прочитать `BOOTSTRAP.md` и `assistant_runbook.yaml` — там описана логика
   выбора датасета, fallback-режим и формат ответа.
2. Перед первым запуском обновить `session_state/live_table_catalog.json` и
   `session_state/live_semantic_index.json` через CLI.
3. Для конкретной выплаты использовать `get_payout(id)` или прицельный SQL по
   `t_payout_extended` (грейн = 1 строка на выплату, исключая зеркальные).
4. Если поле не находится по описанию — переходить в fallback и явно сообщать
   об этом в ответе.
