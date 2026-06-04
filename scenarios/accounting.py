"""Accounting scenario.

Triggers on requests about closing documents (акты, НПД) — final action is
forwarding the customer's message to the dedicated #бухгалтерия chat.
"""

import re

from scenarios.base import Detection, Scenario

_TRIGGER_PATTERN = re.compile(
    r"("
    r"закрывающие\s+документы|"
    r"закрывающих\s+документов|"
    r"пришлите\s+акт[ыа]?|"
    r"пришлите\s+НПД|"
    r"ускорьте\s+подписание|"
    r"отправ(ить|ьте|те)\s+упд|"
    r"упд\s+за\s+\w+|"
    r"упд\s+по\s+эдо|"
    r"по\s+эдо|"
    r"через\s+эдо|"
    r"запрос\s+(акт[аов]?|упд)"
    r")",
    re.IGNORECASE,
)


class AccountingScenario(Scenario):
    name = "accounting"

    def detect(self, text: str) -> Detection | None:
        if not _TRIGGER_PATTERN.search(text):
            return None
        return {
            "name": self.name,
            "notification": f"Запрос в бухгалтерию от клиента: «{text.strip()}»",
            "data": {"client_message": text.strip()},
            "needs_followup": False,
        }
