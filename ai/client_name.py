"""
Извлечение имени клиента из названия Telegram-чата.

Примеры названий и ожидаемый результат:
  "MadeTask&Линперс"                              -> "Линперс"
  "Эктивейт Premier (выплаты лк) & MadeTask"      -> "Эктивейт Premier (выплаты лк)"
  "Profit Entry&MadeTask"                          -> "Profit Entry"
  "MadeTask PROFIT ENTRY - вопросы по выплатам"   -> "PROFIT ENTRY - вопросы по выплатам"
  "Дзен&MadeTask"                                  -> "Дзен"

Если уверенно вычленить не удаётся — возвращаем исходную строку.
"""

import re

_MADETASK_RE = re.compile(r"madetask", re.IGNORECASE)


def extract_client_name(channel_name: str | None) -> str:
    if not channel_name:
        return ""
    name = channel_name.strip()
    if not name:
        return ""

    # 1) Делим по & и выкидываем токены, содержащие "MadeTask"
    if "&" in name:
        parts = [p.strip() for p in name.split("&") if p.strip()]
        non_madetask = [p for p in parts if not _MADETASK_RE.search(p)]
        if non_madetask:
            return " & ".join(non_madetask)

    # 2) Префикс "MadeTask ..." без & — отрезаем
    if _MADETASK_RE.match(name):
        rest = _MADETASK_RE.sub("", name, count=1).lstrip(" -—:|")
        if rest:
            return rest.strip()

    return name
