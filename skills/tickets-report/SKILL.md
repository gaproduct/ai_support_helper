# Скилл: отчёт по тикетам поддержки

Инструкция для агента (Codex, Claude Code или человека). Позволяет собрать
детальный отчёт по тикетам поддержки MadeTask/Remozo за любой период.

## Что нужно для работы

- SSH-доступ: `ssh agusev@gcp-eu-west1b-llm.madetask.tech` (только по имени хоста, не по IP).
- На сервере работает контейнер `support_tickets-webhook-1` с доступом к Postgres.
- Скрипты передаются в контейнер через stdin, результат приходит в stdout:
  `ssh agusev@... "docker exec -i support_tickets-webhook-1 python -" < скрипт.py > вывод`
- Для PDF нужен Chrome на локальной машине.

## Методология E (кратко)

- Источник: таблица `tickets` с `methodology='E'`. Несколько ID исполнителей
  в одном сообщении = 1 тикет. Поле `count` содержит число тикетов строки.
- Теневые дубли Flomni/Telegram исключаются через `shadow_duplicate_dialogs`.
- Исключены: внутренние чаты MT (префиксы client_id `25ee7201`, `ed3c0fcd`),
  чат Т-банка (`fc1e4ebf`, Т-банк это платёжный провайдер, не заказчик),
  рассылки, Wallet-ленты, категории «Тест» и «Другое». Список префиксов
  живёт в `report_filters.py` и `compute_tickets.py`.
- Эффективная категория тикета: `manual_category_override` ->
  депозитный бакет `dialog_fine_subcategory` (перенос в «Пополнение и
  зачисление на баланс») -> `tickets.category`.
- Эффективная подкатегория: `manual_category_override.subcategory` ->
  последний `analysis_results.subcategory` -> `manual_subcategory_override`.

## Порядок сборки отчёта

Все команды из корня репозитория `support_tickets`. Период задаётся
переменными `REPORT_FROM` / `REPORT_TO` в формате YYYY-MM-DD.

### Шаг 1. Проверка качества данных

```bash
ssh agusev@gcp-eu-west1b-llm.madetask.tech \
  "docker exec -i -e REPORT_FROM=2026-10-01 -e REPORT_TO=2026-10-08 \
   support_tickets-webhook-1 python -" < skills/tickets-report/check_quality.py
```

Правила (обязательные, установлены руководителем):
- Тикетов без категории быть не должно (блок 2 = 0).
- Тикетов без подкатегории в сегментируемых категориях быть не должно
  (блок 4 = 0). «Всему нужно давать специфику».
- Тикетов провайдерских чатов быть не должно (блок 5 = 0).

### Шаг 2. Если есть тикеты без подкатегории: ручная разметка

1. Прочитай summary из вывода check_quality.py. Для неоднозначных диалогов
   подними переписку: `SELECT messages_text FROM dialogs WHERE id = <dialog_id>`.
2. Используй только существующие подкатегории. Их список по категории:
   см. запрос распределения в `check_quality.py` или просто текущие значения
   `analysis_results.subcategory` за последние 2 месяца. Новую подкатегорию
   заводи только если кейсов 3+ и они явно не ложатся в существующие.
3. Добавь назначения в словарь `OVERRIDES` файла `apply_manual_subcategory.py`
   (новый блок с комментарием-периодом, по образцу сентября и октября).
   Диалоги про пополнение баланса платформы заказчиком добавляй в
   `DEPOSIT_DIALOGS` там же, они переедут в категорию «Пополнение и
   зачисление на баланс».
4. Примени разметку. ВАЖНО: `apply_manual_subcategory.py` полностью
   перезаписывает таблицу `manual_subcategory_override`, поэтому сразу после
   него ОБЯЗАТЕЛЬНО запусти `apply_september_subcategories.py` (он хранит
   разметку 1-15 сентября и идемпотентен):

```bash
scp apply_manual_subcategory.py agusev@gcp-eu-west1b-llm.madetask.tech:/home/agusev/support_tickets/
ssh agusev@gcp-eu-west1b-llm.madetask.tech "cd /home/agusev/support_tickets \
  && docker exec -i support_tickets-webhook-1 python - < apply_manual_subcategory.py \
  && docker exec -i support_tickets-webhook-1 python - < apply_september_subcategories.py"
```

5. Закоммить изменённый `apply_manual_subcategory.py` в репо, иначе разметка
   потеряется при следующем запуске с чужой машины.
6. Повтори шаг 1 и убедись, что блоки 2, 4, 5 пустые.

### Шаг 3. Сборка HTML

```bash
ssh agusev@gcp-eu-west1b-llm.madetask.tech \
  "docker exec -i -e REPORT_FROM=2026-10-01 -e REPORT_TO=2026-10-08 \
   support_tickets-webhook-1 python -" \
  < skills/tickets-report/build_report.py > ~/Desktop/Октябрь_2026_отчет.html
```

Проверки после сборки:
- файл не пустой, в нём нет строки `(без подкатегории)`;
- суммы в шапке (всего тикетов = заказчики + исполнители) сходятся;
- в топ-15 компаний нет Т-банка.

### Шаг 4. PDF

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$HOME/Desktop/Октябрь_2026_отчет.pdf" \
  "file://$HOME/Desktop/Октябрь_2026_отчет.html"
```

## Известные грабли

- Локальный /tmp на Маке периодически чистится. Рабочие копии скриптов
  держи только в репозитории.
- В `tickets` колонка даты называется `dialog_date`, не `date`.
- В `incoming_messages` нет `dialog_id`, переписку читай из
  `dialogs.messages_text`.
- Шедулер пересчитывает tickets каждое утро (06:00 UTC). Если его образ
  собран из старого кода, он может вернуть исключённые чаты. Проверяй
  блок 5 в check_quality.py; лечится пересборкой образа шедулера.
- Git на сервере без identity. Коммить так:
  `git -c user.name='G A' -c user.email='artgusya8@gmail.com' commit ...`
