"""Поиск и пометка парных получателей Flomni.

Один TG-чат подключён к Flomni двумя каналами. Приходят два вебхука с разными
`receiver` и одинаковым текстом с разницей в доли секунды. Ответы поддержки
уходят только через один канал, у второго в истории одни входящие.

Пара опознаётся по совпадению текста входящего сообщения с разницей до
_WINDOW_SECONDS. Имена чатов не годятся: один канал отдаёт название группы
(«Дзен&MadeTask»), второй имя человека («Dmitriy»).

Канонический получатель тот, у кого больше исходящих. Совпадение по нулям
неоднозначно, такие пары пропускаем: без ответов поддержки выбирать не из чего.

  python flomni_twins.py            # только показать, что нашлось
  python flomni_twins.py --apply    # записать в реестры
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import text

from database import get_session

log = logging.getLogger(__name__)

SOURCE = "flomni"

# Оба вебхука приходят с разницей около 150 мс. Три секунды дают запас на
# задержку доставки и при этом не склеивают разных клиентов.
_WINDOW_SECONDS = 3.0

# Короткие реплики («Добрый день», «done») совпадают у посторонних клиентов.
# Для поиска пар берём только заведомо уникальные тексты.
_MIN_TEXT_LEN = 20

# Одного совпадения мало: тексты могли сойтись случайно. Пару считаем
# доказанной с этого числа общих сообщений.
_MIN_EVIDENCE = 2


def _ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _load_dialogs(db) -> list[tuple[int, str, str]]:
    return db.execute(text("""
        SELECT id, client_id, messages_json
        FROM dialogs
        WHERE source = :src AND messages_json IS NOT NULL
          AND messages_json <> '' AND messages_json <> '[]'
    """), {"src": SOURCE}).fetchall()


def find_pairs(db) -> tuple[dict[tuple[str, str], int], dict[str, Counter]]:
    """Возвращает (пара -> сколько общих сообщений, получатель -> счётчик направлений)."""
    by_text: dict[str, list[tuple[float, str]]] = defaultdict(list)
    direction: dict[str, Counter] = defaultdict(Counter)

    for _, client_id, payload in _load_dialogs(db):
        try:
            messages = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            direction[client_id][msg.get("direction")] += 1
            if msg.get("direction") != "inbound":
                continue
            when = _ts(msg.get("time"))
            body = (msg.get("text") or "").strip()
            if when is None or len(body) < _MIN_TEXT_LEN:
                continue
            by_text[body].append((when, client_id))

    pairs: Counter = Counter()
    for items in by_text.values():
        items.sort()
        for i, (t_i, cid_i) in enumerate(items):
            for t_j, cid_j in items[i + 1:]:
                if t_j - t_i > _WINDOW_SECONDS:
                    break
                if cid_i != cid_j:
                    pairs[tuple(sorted((cid_i, cid_j)))] += 1
    return pairs, direction


def resolve_shadows(
    pairs: dict[tuple[str, str], int], direction: dict[str, Counter]
) -> tuple[dict[str, tuple[str, int]], list[tuple[str, str, int]]]:
    """Раскладывает пары на теневой -> (канонический, доказательства).

    Второй элемент — пары, где выбрать канонического нельзя.
    """
    shadows: dict[str, tuple[str, int]] = {}
    unresolved: list[tuple[str, str, int]] = []

    for (left, right), evidence in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if evidence < _MIN_EVIDENCE:
            continue
        out_left = direction[left]["outbound"]
        out_right = direction[right]["outbound"]
        if out_left == out_right:
            unresolved.append((left, right, evidence))
            continue
        canonical, shadow = (left, right) if out_left > out_right else (right, left)
        # Получатель уже признан каноническим в другой паре — теневым он быть
        # не может, иначе цепочка «A теневой к B, B теневой к C» сломает выборку.
        if shadow in {c for c, _ in shadows.values()}:
            unresolved.append((left, right, evidence))
            continue
        known = shadows.get(shadow)
        if known is None or evidence > known[1]:
            shadows[shadow] = (canonical, evidence)
    return shadows, unresolved


def _names(db) -> dict[str, str]:
    rows = db.execute(text("""
        SELECT DISTINCT ON (client_id) client_id, name
        FROM incoming_messages WHERE source = :src ORDER BY client_id, id DESC
    """), {"src": SOURCE}).fetchall()
    return {cid: name for cid, name in rows}


def _save_pairs(db, shadows: dict[str, tuple[str, int]], names: dict[str, str]) -> None:
    for shadow, (canonical, evidence) in shadows.items():
        db.execute(text("""
            INSERT INTO flomni_twin_receivers
                (shadow_client_id, canonical_client_id, shadow_name, canonical_name,
                 evidence, detected_by)
            VALUES (:s, :c, :sn, :cn, :e, 'scan')
            ON CONFLICT (shadow_client_id) DO UPDATE SET
                canonical_client_id = EXCLUDED.canonical_client_id,
                shadow_name = EXCLUDED.shadow_name,
                canonical_name = EXCLUDED.canonical_name,
                evidence = EXCLUDED.evidence
        """), {
            "s": shadow, "c": canonical, "e": evidence,
            "sn": names.get(shadow), "cn": names.get(canonical),
        })


def _mark_dialogs(db, shadows: dict[str, tuple[str, int]]) -> int:
    """Заносит диалоги теневых получателей в shadow_duplicate_dialogs."""
    marked = 0
    for shadow, (canonical, _) in shadows.items():
        result = db.execute(text("""
            INSERT INTO shadow_duplicate_dialogs
                (dialog_id, client_id, source, dialog_date, canonical_client_id, reason)
            SELECT d.id, d.client_id, d.source, d.dialog_date, :canon, 'flomni_twin_receiver'
            FROM dialogs d
            WHERE d.source = :src AND d.client_id = :shadow
            ON CONFLICT (dialog_id) DO NOTHING
        """), {"src": SOURCE, "shadow": shadow, "canon": canonical})
        marked += result.rowcount or 0
    return marked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать в реестры")
    args = ap.parse_args()

    with get_session() as db:
        pairs, direction = find_pairs(db)
        shadows, unresolved = resolve_shadows(pairs, direction)
        names = _names(db)

        print(f"пар найдено: {len(pairs)}   теневых получателей: {len(shadows)}")
        print(f"пар без явного канона (пропущены): {len(unresolved)}\n")

        header = f"{'общих':>6s}  {'канонический':44s} {'вх/исх':>9s}  теневой"
        print(header)
        print("-" * len(header))
        for shadow, (canonical, evidence) in sorted(shadows.items(), key=lambda kv: -kv[1][1]):
            print(
                f"{evidence:6d}  {(names.get(canonical) or '—')[:42]:44s} "
                f"{direction[canonical]['inbound']}/{direction[canonical]['outbound']:<7} "
                f"{(names.get(shadow) or '—')[:42]} "
                f"{direction[shadow]['inbound']}/{direction[shadow]['outbound']}"
            )

        if unresolved:
            print("\nне разобрано, у обоих одинаково исходящих:")
            for left, right, evidence in unresolved:
                print(f"  {evidence:4d}  {names.get(left) or left}  ~  {names.get(right) or right}")

        if not args.apply:
            print("\nПробный запуск. Ничего не записано, добавьте --apply.")
            return

        _save_pairs(db, shadows, names)
        marked = _mark_dialogs(db, shadows)
        db.commit()
        print(f"\nЗаписано пар: {len(shadows)}. Помечено новых диалогов: {marked}.")


if __name__ == "__main__":
    main()
