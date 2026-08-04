"""Ручное назначение подкатегорий диалогам без AI-подкатегории (разобрано по одиночке
по содержанию переписки) для категорий: Техническая проблема/вопрос, Выплаты,
Запрос документов, Вопросы по работе в сервисе.

- Обычные назначения -> таблица manual_subcategory_override (dialog_id -> subcategory),
  которую отчёт подмешивает через COALESCE(NULLIF(ar.subcategory,''), mso.subcategory).
- Диалоги про зачисление на баланс платформы (заказчик) -> в отдельную категорию
  «Пополнение и зачисление на баланс» через dialog_fine_subcategory (deposit-бакет).
- Разобранный кейс сегментируемой подкатегории «Уточнение причин отказа по платежу»
  дополнительно получает fine-бакет, чтобы детализация сходилась.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import text as t

from database import get_session
import fine_subcategory as fs

# dialog_id -> подкатегория (в рамках её AI-категории)
OVERRIDES = {
    # === Техническая проблема/вопрос ===
    3679: "Вход/пароль/доступ", 3722: "Вход/пароль/доступ",
    4203: "Вход/пароль/доступ", 4253: "Вход/пароль/доступ",
    4286: "Вход/пароль/доступ",
    3687: "Приложение/сайт не работает", 3727: "Приложение/сайт не работает",
    3755: "Приложение/сайт не работает", 4270: "Приложение/сайт не работает",
    4373: "Приложение/сайт не работает",
    3807: "Статус задачи/проверки/транзакции", 4085: "Статус задачи/проверки/транзакции",
    4133: "Статус задачи/проверки/транзакции", 4378: "Статус задачи/проверки/транзакции",
    3672: "Не приходит код/SMS/письмо", 3724: "Не приходит код/SMS/письмо",
    4090: "Не приходит код/SMS/письмо",
    3749: "Интеграция/API/сертификаты", 4000: "Интеграция/API/сертификаты",
    4258: "Интеграция/API/сертификаты",
    # === Выплаты и проблемы с ними === (deposit-кейсы d3801/d4030 обрабатываются ниже)
    2978: "Комиссия/курс/лимиты",     # частота обновления курса, расхождение суммы
    4437: "Комиссия/курс/лимиты",     # произвольная сумма/лимиты/несколько кошельков
    3236: "Изменение реквизитов",     # просьба разблокировать реквизиты
    3618: "Уточнение причин отказа по платежу",  # блок выплаты из-за чужих реквизитов
    # === Запрос документов ===
    3611: "Закрывающие/ЭДО",   # пакет закрывающих, что получает заказчик
    3789: "Закрывающие/ЭДО",   # закрывающие: нет address/details
    4048: "Закрывающие/ЭДО",   # перенос задачи июль/июнь, формирование актов
    4051: "Закрывающие/ЭДО",
    3716: "Договор/оферта",    # перевод сотрудника на полставки
    3735: "Договор/оферта",    # смена договорной модели (ИП как исполнитель)
    4355: "Договор/оферта",    # договор по генподрядной модели, самозанятые
    3786: "Инвойс/акт по выплате",  # уточнение сумм ДМС, выставить счёт
    3829: "Инвойс/акт по выплате",  # средства пришли, сформировать инвойс/акт
    4069: "Отправка документов в ЭДО",  # расхождение УПД, повторная загрузка в ЭДО
    4015: "Реквизиты/данные организации",  # выгрузка списка исполнителей (данные)
    4084: "Реквизиты/данные организации",  # подключить ещё одно ЮЛ
    4251: "Реквизиты/данные организации",  # реквизиты для пополнения, корр. счёт банка
    # === Вопросы по работе в сервисе ===
    3608: "Как пользоваться сервисом", 3828: "Как пользоваться сервисом",
    4075: "Как пользоваться сервисом", 4092: "Как пользоваться сервисом",
    4130: "Как пользоваться сервисом", 4247: "Как пользоваться сервисом",
    4327: "Как пользоваться сервисом", 4329: "Как пользоваться сервисом",
    3630: "Выплаты за рубеж/крипто/нерезиденты",
    3810: "Выплаты за рубеж/крипто/нерезиденты",
    4023: "Выплаты за рубеж/крипто/нерезиденты",
    3706: "Дата принятия задачи",
    3804: "Обороты, налоги, партнёрство (КАМ)", 3805: "Обороты, налоги, партнёрство (КАМ)",
    4144: "Обороты, налоги, партнёрство (КАМ)", 4145: "Обороты, налоги, партнёрство (КАМ)",
    4146: "Обороты, налоги, партнёрство (КАМ)", 4147: "Обороты, налоги, партнёрство (КАМ)",
    4206: "Обороты, налоги, партнёрство (КАМ)",
    4058: "API/интеграция/безопасность",
    4279: "Постановка задач и отчёты", 4294: "Постановка задач и отчёты",
    4298: "Постановка задач и отчёты",
}

# Диалоги про зачисление на баланс платформы (заказчик) -> deposit-категория
DEPOSIT_DIALOGS = [3801, 4030]
# Разобранные кейсы сегментируемых подкатегорий -> нужен fine-бакет для сходимости
REFUSAL_DIALOGS = [3618]

s = get_session()
s.execute(t("""
    CREATE TABLE IF NOT EXISTS manual_subcategory_override (
        dialog_id INTEGER PRIMARY KEY,
        subcategory TEXT NOT NULL
    )
"""))
s.execute(t("DELETE FROM manual_subcategory_override"))
for did, sub in OVERRIDES.items():
    s.execute(t("INSERT INTO manual_subcategory_override(dialog_id, subcategory) "
                "VALUES (:d, :s)"), {"d": did, "s": sub})
# старая узкая таблица больше не нужна
s.execute(t("DROP TABLE IF EXISTS tech_subcategory_override"))
s.commit()


def _blob(did):
    mj = s.execute(t("SELECT messages_json FROM dialogs WHERE id=:d"), {"d": did}).scalar()
    summ = s.execute(t("SELECT summary FROM analysis_results WHERE dialog_id=:d "
                       "ORDER BY id DESC LIMIT 1"), {"d": did}).scalar()
    rat = s.execute(t("SELECT rationale FROM analysis_results WHERE dialog_id=:d "
                      "ORDER BY id DESC LIMIT 1"), {"d": did}).scalar()
    try:
        msgs = json.loads(mj or "[]")
    except (ValueError, TypeError):
        msgs = []
    return fs.build_blob(msgs, summ, rat)


now = datetime.now(timezone.utc)
UPSERT = """
INSERT INTO dialog_fine_subcategory (dialog_id, category, subcategory, fine_bucket, computed_at)
VALUES (:dialog_id, :category, :subcategory, :fine_bucket, :computed_at)
ON CONFLICT (dialog_id) DO UPDATE SET
    category=EXCLUDED.category, subcategory=EXCLUDED.subcategory,
    fine_bucket=EXCLUDED.fine_bucket, computed_at=EXCLUDED.computed_at
"""
for did in DEPOSIT_DIALOGS:
    s.execute(t(UPSERT), {"dialog_id": did, "category": "Выплаты и проблемы с ними",
                          "subcategory": fs.HANGUP_SUB, "fine_bucket": fs.DEPOSIT_BUCKET,
                          "computed_at": now})
for did in REFUSAL_DIALOGS:
    bucket = fs.classify(fs.REFUSAL_SUB, _blob(did))
    s.execute(t(UPSERT), {"dialog_id": did, "category": "Выплаты и проблемы с ними",
                          "subcategory": fs.REFUSAL_SUB, "fine_bucket": bucket,
                          "computed_at": now})
    print(f"d{did} refusal fine-bucket -> {bucket}")
s.commit()

n = s.execute(t("SELECT COUNT(*) FROM manual_subcategory_override")).scalar()
print(f"manual_subcategory_override rows: {n}")

# Проверка: не осталось ли «без подкатегории» ни в одной сегментируемой категории
CATS = ["Техническая проблема/вопрос", "Выплаты и проблемы с ними",
        "Запрос документов", "Вопросы по работе в сервисе"]
for cat in CATS:
    left = s.execute(t("""
        WITH ar AS (SELECT DISTINCT ON (dialog_id) dialog_id, subcategory
                    FROM analysis_results ORDER BY dialog_id, id DESC)
        SELECT DISTINCT tk.dialog_id
          FROM tickets tk JOIN ar ON ar.dialog_id=tk.dialog_id
          LEFT JOIN manual_subcategory_override mo ON mo.dialog_id=tk.dialog_id
          LEFT JOIN dialog_fine_subcategory dfs ON dfs.dialog_id=tk.dialog_id AND dfs.fine_bucket=:dep
         WHERE tk.methodology='E' AND tk.dialog_date BETWEEN '2026-07-01' AND '2026-07-31'
           AND tk.category=:c AND (ar.subcategory IS NULL OR ar.subcategory='')
           AND mo.dialog_id IS NULL AND dfs.dialog_id IS NULL
    """), {"c": cat, "dep": fs.DEPOSIT_BUCKET}).fetchall()
    print(f"  uncovered in «{cat}»: {len(left)} {[r[0] for r in left]}")
