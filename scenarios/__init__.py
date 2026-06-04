"""Scenarios package — central registry + detection entrypoint.

Adding a new scenario:
  1. Create scenarios/<name>.py with a Scenario subclass.
  2. Import it here and add an instance to ALL_SCENARIOS.
  3. (UI) wire its `name` to a banner/style in test.html / chat.html / slack.html.
"""

from scenarios.accounting import AccountingScenario
from scenarios.base import Detection, Scenario
from scenarios.compliance import VerificationScenario
from scenarios.finance import BalanceTopupScenario
from scenarios.payout_context import PayoutContextScenario

# Order matters: the first matching scenario wins.
ALL_SCENARIOS: list[Scenario] = [
    PayoutContextScenario(),
    BalanceTopupScenario(),
    VerificationScenario(),
    AccountingScenario(),
]

_BY_NAME: dict[str, Scenario] = {s.name: s for s in ALL_SCENARIOS}


def detect_scenario(text: str) -> Detection | None:
    """Return Detection of the first matching scenario, or None."""
    for scenario in ALL_SCENARIOS:
        result = scenario.detect(text)
        if result is not None:
            return result
    return None


def get_scenario(name: str) -> Scenario | None:
    """Return scenario instance by name (used to resolve follow-ups)."""
    return _BY_NAME.get(name)


__all__ = ["Detection", "Scenario", "detect_scenario", "get_scenario", "ALL_SCENARIOS"]
