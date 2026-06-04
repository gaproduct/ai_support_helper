"""Base class for support scenarios."""

from typing import TypedDict


class Detection(TypedDict, total=False):
    name: str               # scenario identifier ("compliance" / "finance" / ...)
    notification: str       # text shown to operators / sent to dedicated channel,
                            # OR — for informational scenarios — the answer itself
    data: dict              # arbitrary scenario-specific payload (e.g. {"email": ...})
    needs_followup: bool    # True when extra input from the client is required
    followup_hint: str      # message asking the client for the missing info
    informational: bool     # True = the scenario produces an answer for the client
                            # itself (no routing to dedicated channel). UI/notifier
                            # should render `notification` as the AI assistant reply.


class Scenario:
    """Abstract scenario.

    Subclasses set `name` and implement `detect()`. If a scenario can ask for
    follow-up input (e.g. an email), it also implements `complete_followup()`.
    """

    name: str = ""

    def detect(self, text: str) -> Detection | None:
        """Return Detection dict if `text` triggers this scenario, else None."""
        raise NotImplementedError

    def complete_followup(self, data: dict, followup_text: str) -> Detection | None:
        """
        Resolve a previously requested follow-up.
        Return final Detection on success or None to keep waiting.
        """
        return None
