-- Кабинет оператора: индекс диалогов для поиска похожих кейсов.
--
-- Две таблицы наполняет джоба dialog_index.py, она идёт в планировщике сразу
-- после ai_analysis. Считаем только то, чего ещё нет, поэтому повторный запуск
-- дешёвый.
--
-- Похожие кейсы ищем в два шага: сначала отбор по категории из analysis_results,
-- потом косинусная близость по вектору. После отбора кандидатов остаются сотни,
-- поэтому pgvector не нужен, косинус считается в питоне.
--
-- Вектор храним как real[]. Это 512 измерений от text-embedding-3-small
-- (модель умеет отдавать укороченный вектор), примерно 2 КБ на диалог.

BEGIN;

CREATE TABLE IF NOT EXISTS dialog_embeddings (
    dialog_id   integer PRIMARY KEY REFERENCES dialogs (id) ON DELETE CASCADE,
    vec         real[] NOT NULL,
    model       text   NOT NULL,
    -- Хеш проиндексированного текста. Диалог дописывается со временем, и по
    -- расхождению хеша джоба понимает, что вектор устарел и его надо пересчитать.
    source_hash text   NOT NULL,
    computed_at timestamp NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dialog_outcomes (
    dialog_id   integer PRIMARY KEY REFERENCES dialogs (id) ON DELETE CASCADE,
    -- «Чем закончилось»: 1-3 предложения о том, что поддержка сделала и с каким
    -- результатом. Отдельно от analysis_results.summary, там пересказ обращения,
    -- а не решение. Держим в своей таблице, чтобы перезапуск ai_analysis не тёр.
    outcome     text,
    model       text   NOT NULL,
    source_hash text   NOT NULL,
    computed_at timestamp NOT NULL DEFAULT now()
);

COMMIT;
