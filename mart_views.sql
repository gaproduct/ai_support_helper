-- Витрины для выгрузки в Superset.
--
-- Зачем они нужны. В сырых таблицах шесть правил, которые аналитик не угадает:
-- методология тикетов, вес строки, дедупликация разметки, эффективная категория,
-- теневые дубли и канонические названия компаний. Каждое из них меняет цифру.
-- Здесь все шесть уже зашиты, поэтому витрину можно просто группировать.
--
-- Текст переписки наружу не отдаём: в Superset круг доступа шире. Наружу идут
-- категории, компании, даты, метрики скорости и краткое описание от модели.
--
-- Применить: psql -U postgres -d support_tickets -f mart_views.sql

-- Общая часть: атрибуты тикета на уровне диалога.
-- Материализуем один раз, чтобы обе витрины считали категорию одинаково.
CREATE OR REPLACE VIEW mart_ticket_base AS
WITH ar AS (
    -- У диалога может быть несколько прогонов разметки. Берём последний.
    -- Физические дубли вычищены 25.08.2026, но ai_analysis вставляет без upsert,
    -- поэтому защита остаётся в витрине.
    SELECT DISTINCT ON (dialog_id)
           dialog_id, summary, subcategory, sentiment, priority, resolution
      FROM analysis_results
     ORDER BY dialog_id, id DESC
),
alias AS (
    -- Справочник приводим к нижнему регистру. Пара алиасов различается только
    -- регистром, без DISTINCT они размножили бы строки тикетов.
    SELECT DISTINCT ON (lower(btrim(alias)))
           lower(btrim(alias)) AS key, canonical
      FROM company_aliases
     ORDER BY lower(btrim(alias)), canonical
),
raw AS (
    SELECT t.dialog_id,
           t.dialog_date,
           t.source,
           t.side,
           t.count AS ticket_count,
           t.executor_emails,
           d.client_id,
           d.chat_name,
           COALESCE(ovr.category,
                    CASE WHEN fsc.fine_bucket = 'Пополнение/депозит: зачисление на баланс платформы (заказчик)'
                         THEN 'Пополнение и зачисление на баланс' END,
                    t.category) AS category,
           COALESCE(ovr.subcategory, NULLIF(ar.subcategory, ''), mso.subcategory) AS raw_subcategory,
           fsc.fine_bucket,
           t.company AS company_raw,
           -- «Unknown» это заглушка парсера, а не компания. Все отчёты её гасят,
           -- иначе она попадает в топ заказчиков наравне с настоящими.
           NULLIF(COALESCE(al.canonical, t.company), 'Unknown') AS company,
           ar.summary,
           ar.sentiment,
           ar.priority,
           ar.resolution
      FROM tickets t
      JOIN dialogs d ON d.id = t.dialog_id
      LEFT JOIN ar ON ar.dialog_id = t.dialog_id
      LEFT JOIN dialog_fine_subcategory fsc ON fsc.dialog_id = t.dialog_id
      LEFT JOIN manual_category_override ovr ON ovr.dialog_id = t.dialog_id
      LEFT JOIN manual_subcategory_override mso ON mso.dialog_id = t.dialog_id
      LEFT JOIN alias al ON al.key = lower(btrim(t.company))
     WHERE t.methodology = 'E'
       -- Теневые дубли: тот же чат Telegram, пришедший вторым каналом Flomni.
       -- 211 строк на 25.08.2026. В отчётах их нет, здесь тоже быть не должно.
       AND NOT EXISTS (SELECT 1 FROM shadow_duplicate_dialogs s
                        WHERE s.dialog_id = t.dialog_id)
)
SELECT dialog_id,
       dialog_date,
       source,
       side,
       ticket_count,
       category,
       -- Две формулировки одной проблемы верификации сводятся в одну подкатегорию.
       CASE raw_subcategory
            WHEN 'Запрос на верификацию' THEN 'Проблемы с прохождением верификации'
            WHEN 'Ошибка прохождения'    THEN 'Проблемы с прохождением верификации'
            ELSE raw_subcategory
       END AS subcategory,
       fine_bucket,
       company,
       company_raw,
       client_id,
       chat_name,
       executor_emails,
       summary,
       sentiment,
       priority,
       resolution
  FROM raw
 -- Служебные категории тикетами не считаются. compute_tickets отсекает их по
 -- разметке модели, но ручной оверрайд ставится поверх и проходит мимо фильтра.
 WHERE category NOT IN ('Тест', 'Другое', 'Рассылка', 'Дубль', 'Без запроса');

COMMENT ON VIEW mart_ticket_base IS
'Тикеты методологии E: одна строка на диалог. Категория эффективная, компания каноническая, теневые дубли исключены. Вес строки в ticket_count, а не 1.';


-- Витрина 1: тикеты. Одна строка на диалог, плюс агрегаты по инцидентам.
CREATE OR REPLACE VIEW mart_tickets AS
SELECT b.*,
       m.incidents,
       m.messages_total,
       m.first_response_seconds,
       m.resolved_incidents
  FROM mart_ticket_base b
  LEFT JOIN (
       SELECT dialog_id,
              count(*)                                   AS incidents,
              sum(n_messages)                            AS messages_total,
              -- По диалогу берём первый ответ на первом инциденте.
              min(first_response_seconds)                AS first_response_seconds,
              count(*) FILTER (WHERE status = 'resolved') AS resolved_incidents
         FROM ticket_resolution_metrics
        GROUP BY dialog_id
  ) m ON m.dialog_id = b.dialog_id;

COMMENT ON VIEW mart_tickets IS
'Главная витрина: одна строка на тикет-диалог. Для счёта тикетов SUM(ticket_count), не COUNT(*).';


-- Витрина 2: инциденты. Одна строка на обращение внутри диалога, метрики скорости.
-- Диалог режется на инциденты по паузе больше 24 часов перед репликой клиента.
CREATE OR REPLACE VIEW mart_incidents AS
SELECT m.dialog_id,
       m.incident_index,
       b.dialog_date,
       b.source,
       b.side,
       b.category,
       b.subcategory,
       b.company,
       m.client_id,
       m.started_at,
       m.first_response_at,
       m.resolved_at,
       m.first_response_seconds,
       m.resolution_seconds,
       -- Только рабочие часы: пн-пт 10:00-19:00 МСК.
       m.resolution_working_seconds,
       m.handoff_seconds_total,
       m.handoff_by_dept,
       m.status,
       m.n_messages
  FROM ticket_resolution_metrics m
  JOIN mart_ticket_base b ON b.dialog_id = m.dialog_id;

COMMENT ON VIEW mart_incidents IS
'Инциденты внутри тикетов методологии E. Гранулярность обращение, не диалог. Скорость первого ответа и решения считать здесь.';


-- Витрина 3: длинный ряд с января. Гранулярность ДИАЛОГ, не тикет.
--
-- Январь-май живёт по другой методологии: тикетов там не считали, метрик
-- скорости нет, таксономия была шире на 13 категорий. Свести всё это к тикетам
-- нельзя, а к диалогам и категориям можно. Отсюда отдельная витрина: она
-- отвечает на вопрос «сколько обращений и о чём», и только на него.
--
-- Складывать её с mart_tickets нельзя. Там тикеты, здесь диалоги.
CREATE OR REPLACE VIEW mart_dialogs_history AS
WITH alias AS (
    SELECT DISTINCT ON (lower(btrim(alias)))
           lower(btrim(alias)) AS key, canonical
      FROM company_aliases
     ORDER BY lower(btrim(alias)), canonical
),
archive AS (
    SELECT a.dialog_date,
           a.source,
           a.may_side AS side,
           -- Старые категории сводим к нынешним. Разбор по текстам обращений:
           -- SEPA/SWIFT и заблокированные реквизиты это проблемы с выплатой,
           -- НДФЛ это вопрос о налоговом статусе, EOR это вопрос о сервисе,
           -- «закрывашки в ЭДО» это запрос документов.
           CASE a.may_category
                WHEN 'Проблема KYC'                     THEN 'KYC'
                WHEN 'Дублирование KYC'                 THEN 'KYC'
                WHEN 'Запрос документов не бух'         THEN 'Запрос документов'
                WHEN 'Вопросы по числам отправки закрывашек в эдо заказчику/поторопить бухгалтерию'
                                                        THEN 'Запрос документов'
                WHEN 'SEPA/SWIFT'                       THEN 'Выплаты и проблемы с ними'
                WHEN 'Реквизиты заблокированы'          THEN 'Выплаты и проблемы с ними'
                WHEN 'НДФЛ'                             THEN 'Налоговый статус исполнителя'
                WHEN 'ИП РФ'                            THEN 'Статус ИП РФ/Самозанятого'
                WHEN 'EOR'                              THEN 'Вопросы по работе в сервисе'
                WHEN 'Предложение (маркетинг, сотрудничество, банкинг, итд)'
                                                        THEN 'Потенциальный клиент'
                WHEN 'Функциональность сервиса и возможности выплат'
                                                        THEN 'Функциональность сервиса и возможность выплат'
                ELSE a.may_category
           END AS category,
           NULLIF(COALESCE(al.canonical, a.company), 'Unknown') AS company,
           a.chat_name,
           a.ai_summary AS summary,
           'jan_may'::text AS data_era
      FROM analytics_archive_jan_may a
      LEFT JOIN alias al ON al.key = lower(btrim(a.company))
     -- Окна выгрузок unified и live пересекаются с 24 по 30 апреля, но строки
     -- не дублируются: unified это bitrix и chatapp, live это flomni.
     -- Совпадающих чатов за эту неделю ноль, поэтому берём обе целиком.
),
current_era AS (
    SELECT dialog_date, source, side, category, company, chat_name, summary,
           'jun_aug'::text AS data_era
      FROM mart_ticket_base
)
SELECT * FROM archive
 WHERE category NOT IN ('Тест', 'Другое', 'Рассылка', 'Дубль', 'Без запроса')
UNION ALL
SELECT * FROM current_era;

COMMENT ON VIEW mart_dialogs_history IS
'Длинный ряд с января по август на уровне диалогов. Категории января-мая сведены к нынешним. Тикетов и метрик скорости здесь нет: до июня их не считали. С mart_tickets не складывается.';
