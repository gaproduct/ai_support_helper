"""Ручные корректировки AI-мисклассификации на уровне диалога.
Переносит конкретные диалоги в правильную категорию/подкатегорию.
Применяется в отчёте через COALESCE(ovr.category, ...). Идемпотентно."""
from datetime import datetime, timezone
from sqlalchemy import text as t
from database import get_session

# (dialog_id, category, subcategory | None) — разобрано вручную из содержимого.
# Это диалоги, ошибочно отнесённые AI к «Запрос документов / Договор/оферта».
OVERRIDES = [
    (3162, "Выплаты и проблемы с ними", "Зависание в обработке"),          # застрял вывод на карту
    (4055, "Выплаты и проблемы с ними", "Уточнение причин отказа по платежу"),  # ошибочный платёж/разблокировка
    (4112, "KYC", "Запрос на верификацию"),                                # провайдер запросил документы для выплаты
    (4129, "KYC", "Запрос на верификацию"),                                # проверка ООО, комплаенс
    (4153, "Зачисление платежа на баланс", None),                          # пополнение через стороннюю орг
    (4430, "Запрос документов", "Инвойс/акт по выплате"),                   # корректировка инвойса
    (3296, "Запрос документов", "Документы для визы/ВНЖ/банка"),            # документ для налоговой (визовый кейс)
    (3601, "Другое", None),                                                 # подтверждение окладов сотрудников
    (3723, "Другое", None),                                                 # резюме/референс кандидата (HR)
]

DDL = """
CREATE TABLE IF NOT EXISTS manual_category_override (
    dialog_id   INTEGER PRIMARY KEY REFERENCES dialogs(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    subcategory TEXT,
    computed_at TIMESTAMPTZ NOT NULL
);
"""

UPSERT = """
INSERT INTO manual_category_override (dialog_id, category, subcategory, computed_at)
VALUES (:dialog_id, :category, :subcategory, :computed_at)
ON CONFLICT (dialog_id) DO UPDATE SET
    category=EXCLUDED.category, subcategory=EXCLUDED.subcategory,
    computed_at=EXCLUDED.computed_at
"""


def main() -> None:
    s = get_session()
    s.execute(t(DDL))
    now = datetime.now(timezone.utc)
    for did, cat, sub in OVERRIDES:
        s.execute(t(UPSERT), {"dialog_id": did, "category": cat,
                              "subcategory": sub, "computed_at": now})
    s.commit()
    print(f"manual_category_override: upserted {len(OVERRIDES)} rows.")


if __name__ == "__main__":
    main()
