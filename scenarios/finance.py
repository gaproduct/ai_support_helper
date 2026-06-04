"""Finance / balance top-up scenario.

Triggers when the customer reports a money transfer or asks when funds will be
credited to the platform balance.
"""

import re

from scenarios.base import Detection, Scenario

_TRIGGER_PATTERN = re.compile(
    r"("
    r"пополнение\s+баланса|пополнить\s+баланс|пополнил[иа]?\s+баланс|"
    r"зачислите\s+(пожалуйста\s+)?на\s+баланс|зачислите\s+оперативно|"
    r"когда\s+(ожидать\s+|будет\s+)?зачислени[ея]|когда\s+ожидать\s+пополнение|"
    r"средства\s+(до\s+сих\s+пор\s+)?не\s+поступили|деньги\s+не\s+поступили|"
    r"ссылка\s+на\s+(транзакцию|платежный\s+документ)|"
    r"деньги\s+отправили|средства\s+отправили|"
    # «просим/прошу/просьба зачислить … (денежные средства) … на баланс»
    r"(просим|прошу|просьба)\s+зачислить.{0,60}баланс|"
    # «пополните деньги/средства (на) баланс», «пополните баланс»
    r"пополните?\s+(деньги\s+|средства\s+)?(на\s+)?баланс|"
    # «зачисления нет», «нет зачисления»
    r"зачислени[яе]\s+нет|нет\s+зачислени[яе]"
    r")",
    re.IGNORECASE,
)


class BalanceTopupScenario(Scenario):
    name = "finance"

    def detect(self, text: str) -> Detection | None:
        if not _TRIGGER_PATTERN.search(text):
            return None
        return {
            "name": self.name,
            "notification": f"Заказчик просит пополнить баланс, его исходное сообщение: «{text.strip()}»",
            "data": {},
            "needs_followup": False,
        }
