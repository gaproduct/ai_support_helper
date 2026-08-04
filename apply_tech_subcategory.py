"""Явное назначение подкатегорий диалогам категории «Техническая проблема/вопрос»,
у которых AI не проставил подкатегорию (были «без подкатегории»).
Каждый диалог разобран по одиночке по содержанию переписки.
Создаёт/наполняет таблицу tech_subcategory_override (dialog_id -> subcategory),
которую отчёт подмешивает через COALESCE(NULLIF(ar.subcategory,''), ov.subcategory).
"""
from sqlalchemy import text as t
from database import get_session

# dialog_id -> назначенная подкатегория (из числа уже существующих в категории +
# новая «Интеграция/API/сертификаты» для интеграционных/mTLS/сертификатных вопросов)
OVERRIDES = {
    # Вход/пароль/доступ
    3679: "Вход/пароль/доступ",   # cannot log in, website unavailable
    3722: "Вход/пароль/доступ",   # забыла пароль, восстановление доступа
    4203: "Вход/пароль/доступ",   # отказ при входе через email и телефон
    4253: "Вход/пароль/доступ",   # не открывается задача, оператор советует сбросить пароль
    4286: "Вход/пароль/доступ",   # не может закончить регистрацию (4 пункт)
    # Приложение/сайт не работает
    3687: "Приложение/сайт не работает",  # как скачать (установка/использование)
    3727: "Приложение/сайт не работает",  # поле ФИО выделяется красным и сбрасывается
    3755: "Приложение/сайт не работает",  # система показывает ложное предупреждение по ФИО
    4270: "Приложение/сайт не работает",  # не умеет пользоваться приложением, установка
    4373: "Приложение/сайт не работает",  # чат не уходит, некорректный шаблон реестра
    # Статус задачи/проверки/транзакции
    3807: "Статус задачи/проверки/транзакции",  # нет ответов на обращения
    4085: "Статус задачи/проверки/транзакции",  # аккаунт не верифицируется
    4133: "Статус задачи/проверки/транзакции",  # не может пройти верификацию, документы не принимаются
    4378: "Статус задачи/проверки/транзакции",  # время обработки запроса
    # Не приходит код/SMS/письмо
    3672: "Не приходит код/SMS/письмо",  # не пришёл код при регистрации
    3724: "Не приходит код/SMS/письмо",  # не приходит код при прикреплении карты
    4090: "Не приходит код/SMS/письмо",  # не поступил ответ с указанной почты
    # Интеграция/API/сертификаты (новая подкатегория)
    3749: "Интеграция/API/сертификаты",  # автоматизация сбора выписок/данных по счетам
    4000: "Интеграция/API/сертификаты",  # проблемы с сертификатом, уведомления об изменениях
    4258: "Интеграция/API/сертификаты",  # статус по mTLS
}

s = get_session()
s.execute(t("""
    CREATE TABLE IF NOT EXISTS tech_subcategory_override (
        dialog_id INTEGER PRIMARY KEY,
        subcategory TEXT NOT NULL
    )
"""))
s.execute(t("DELETE FROM tech_subcategory_override"))
for did, sub in OVERRIDES.items():
    s.execute(t("INSERT INTO tech_subcategory_override(dialog_id, subcategory) "
                "VALUES (:d, :s)"), {"d": did, "s": sub})
s.commit()

n = s.execute(t("SELECT COUNT(*) FROM tech_subcategory_override")).scalar()
print(f"tech_subcategory_override rows: {n}")

# Проверка: не осталось ли непокрытых «без подкатегории» диалогов в июле (метод E)
left = s.execute(t("""
    WITH ar AS (SELECT DISTINCT ON (dialog_id) dialog_id, subcategory
                FROM analysis_results ORDER BY dialog_id, id DESC)
    SELECT DISTINCT tk.dialog_id
      FROM tickets tk JOIN ar ON ar.dialog_id=tk.dialog_id
      LEFT JOIN tech_subcategory_override ov ON ov.dialog_id=tk.dialog_id
     WHERE tk.methodology='E' AND tk.dialog_date BETWEEN '2026-07-01' AND '2026-07-31'
       AND tk.category='Техническая проблема/вопрос'
       AND (ar.subcategory IS NULL OR ar.subcategory='')
       AND ov.dialog_id IS NULL
""")).fetchall()
print(f"uncovered (без подкатегории) after override: {len(left)}", [r[0] for r in left])
