"""Ручные переопределения стороны тикета (customer/executor).

Эвристика стороны иногда ошибается: компании, оплачивающие счета на
пополнение баланса, попадают в executor. Здесь перечислены диалоги с
проверенной вручную стороной. compute_tickets.compute() применяет эти
переопределения после каждого пересчёта, поэтому ежедневный пересчёт
7-дневного окна их не откатывает. Идемпотентно."""
from datetime import datetime, timezone
from sqlalchemy import text as t
from database import get_session

# (dialog_id, side) — разобрано вручную из переписки.
OVERRIDES = [
    # Октябрь 2026: пополнение баланса компаниями (оплата счетов) —
    # заказчики, ошибочно размеченные эвристикой как исполнители.
    (9168, "customer"), (9193, "customer"), (9349, "customer"),
    (9415, "customer"), (9470, "customer"), (9571, "customer"),
    (9579, "customer"), (9591, "customer"), (9600, "customer"),
    (9645, "customer"), (9667, "customer"), (9707, "customer"),
    (9712, "customer"), (9713, "customer"), (9716, "customer"),
    (9718, "customer"), (9724, "customer"), (9741, "customer"),
    (9753, "customer"), (9758, "customer"), (9761, "customer"),
    (9770, "customer"), (9801, "customer"), (9865, "customer"),
    (9877, "customer"), (9894, "customer"), (9920, "customer"),
    (10025, "customer"), (10114, "customer"), (10230, "customer"),
    (10239, "customer"), (10245, "customer"), (10287, "customer"),
    (10297, "customer"), (10414, "customer"), (10430, "customer"),
    (10467, "customer"), (10544, "customer"), (10604, "customer"),
    (10638, "customer"),
    # Заказчик из ОАЭ: комиссии и пути выплат в Азербайджан (не пополнение).
    (9636, "customer"),
]

DDL = """
CREATE TABLE IF NOT EXISTS manual_side_override (
    dialog_id   INTEGER PRIMARY KEY REFERENCES dialogs(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL
);
"""

UPSERT = """
INSERT INTO manual_side_override (dialog_id, side, computed_at)
VALUES (:dialog_id, :side, :computed_at)
ON CONFLICT (dialog_id) DO UPDATE SET
    side=EXCLUDED.side, computed_at=EXCLUDED.computed_at
"""


def main() -> None:
    s = get_session()
    s.execute(t(DDL))
    now = datetime.now(timezone.utc)
    for did, side in OVERRIDES:
        s.execute(t(UPSERT), {"dialog_id": did, "side": side,
                              "computed_at": now})
    s.commit()
    print(f"manual_side_override: upserted {len(OVERRIDES)} rows.")


if __name__ == "__main__":
    main()
