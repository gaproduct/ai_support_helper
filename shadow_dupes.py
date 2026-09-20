"""
Реестр теневых дублей диалогов (client-only копии реальных переписок, где
сторона саппорта не записана). Такие строки физически остаются в `dialogs`,
но во ВСЕЙ аналитике тикетов/метрик их нужно исключать, иначе они задваивают
клиентов и раздувают незавершёнку.

Помечаются в таблице shadow_duplicate_dialogs (заполняется при дедупе).
Использование:
    from shadow_dupes import shadow_dialog_ids, without_shadows
    excl = shadow_dialog_ids(session)
    rows = [d for d in rows if d.id not in excl]
"""
from __future__ import annotations

from sqlalchemy import text


def shadow_dialog_ids(session) -> set[int]:
    """Множество dialog_id, помеченных как теневые дубли."""
    rows = session.execute(text("SELECT dialog_id FROM shadow_duplicate_dialogs")).fetchall()
    return {r[0] for r in rows}


def without_shadows(session, dialogs: list) -> list:
    """Отфильтровать список Dialog-объектов, убрав теневые дубли."""
    excl = shadow_dialog_ids(session)
    return [d for d in dialogs if d.id not in excl]
