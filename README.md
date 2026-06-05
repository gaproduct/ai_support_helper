# AI Support Helper

Внутренний пайплайн поддержки клиентов на базе Flomni / ChatApp / Bitrix24.
Собирает входящие сообщения из всех каналов, складывает в единую БД,
прогоняет через AI-разбор (Claude / OpenAI) и помогает операторам через
Slack-черновики ответов и сценарии (compliance / finance / accounting).

## Что внутри

### Entry points (корень)

| Файл | Назначение |
|---|---|
| `main.py` | FastAPI-приложение: webhooks (Flomni, ChatApp, Slack), KB-поиск, чат-UI |
| `scheduler.py` | Daily-джобы (APScheduler): забор истории, AI-разбор, атрибуция компаний |
| `build_reports.py` | Генерация аналитических отчётов за Jan-May (HTML/XLS) |

### Пакеты

| Пакет | Назначение |
|---|---|
| `core/` | Базовая инфраструктура: `config` (env settings), `database` (SQLAlchemy модели) |
| `ai/` | AI / NLP: `analysis`, `draft`, `response`, `auto_response`, `topic_detection`, `client_name` |
| `sources/` | Клиенты внешних каналов: `chatapp_client`, `chatapp_history`, `flomni_history` |
| `attribution/` | Атрибуция компании по email/группе: `pipeline`, `extract_emails`, `from_group_name`, `resolve_companies` |
| `slack_integration/` | Slack: `client`, `drafts`, `chat_session` |
| `kb/` | Knowledge base — обучение и хранение чанков (`train`) |
| `tools/` | Внутренние утилиты: `db_query` (NL → SQL для Superset) |
| `webhooks/` | Обработчики входящих webhook'ов (signature verification, draft посты) |
| `scenarios/` | Базовые AI-сценарии: compliance, finance, accounting, payout_context |
| `payouts_agent/` | Superset-клиент для analytics-запросов LLM-ассистентом |
| `api/` | Внутренние HTTP-роутеры (KB train, chat, search, db_query) |
| `ui/` | FastAPI Jinja-шаблоны (chat, slack drafts, train, kb, test) |
| `migrations/` | Идемпотентные SQL-миграции для развертки PostgreSQL с нуля |

## Архитектура (high level)

```
┌──────────┐   webhook    ┌──────────┐   AI разбор   ┌──────────────┐
│ Flomni / │ ───────────▶ │ FastAPI  │ ────────────▶ │ PostgreSQL   │
│ ChatApp  │              │ (main.py)│               │ (dialogs +   │
│ Bitrix24 │              └─────┬────┘               │  analysis)   │
└──────────┘                    │                    └──────┬───────┘
                                │ post draft                │
                                ▼                           │ KB embed
                          ┌──────────┐                      │
                          │  Slack   │                      ▼
                          │ #drafts  │              ┌──────────────┐
                          └──────────┘              │  Supabase    │
                                                    │ (KB vector)  │
                                                    └──────────────┘
       ┌─────────────────────────────────────────────────────┐
       │ APScheduler (scheduler.py) — daily 02:00-05:00 UTC: │
       │  • flomni_history    • chatapp_history              │
       │  • ai_analysis       • company_attribution          │
       └─────────────────────────────────────────────────────┘
```

## Развертка

### 1. Подготовить окружение

```bash
git clone https://github.com/gaproduct/ai_support_helper.git
cd ai_support_helper
cp .env.example .env
# заполнить ключи: FLOMNI, OPENAI, SLACK, CHATAPP, SUPABASE
```

### 2. Применить миграции БД

Подробности — см. [`migrations/README.md`](./migrations/README.md).

```bash
docker compose up -d db
for f in migrations/*.sql; do
    docker compose exec -T db psql -U postgres -d support_tickets < "$f"
done
```

### 3. Поднять сервисы

```bash
docker compose up -d           # webhook (8000) + scheduler + db
docker compose logs -f         # смотрим, что всё ок
```

`webhook` слушает порт 8000 (FastAPI), `scheduler` крутит cron-задачи.

## Локальная разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## Аналитика

Готовый дамп таблицы за Jan-May 2026 (5948 тикетов) собирается
скриптом `build_reports.py` и публикуется в `exports/`
(не коммитится — см. `.gitignore`). Описание полей и SQL-рецепты —
в `exports/README.md` (генерируется при сборке).

## Структура зависимостей

- Python 3.12+
- PostgreSQL 16
- Анализ: OpenAI / Anthropic API
- Vector store: Supabase (pgvector)
- Slack: Bolt / Web API
- BI-агент: Superset REST API
