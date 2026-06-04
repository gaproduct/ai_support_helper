"""Compliance / verification scenario.

Triggers on messages reporting verification failures or explicit verification
requests. If the message includes an email — the scenario completes immediately.
Otherwise the scenario asks for the email as a follow-up.
"""

import re

from scenarios.base import Detection, Scenario

_TRIGGER_PATTERN = re.compile(
    r"("
    r"не\s+(могу|можем|может|смог|смогла|смогли|смогло|удалось|получается|получилось)\s+(вер[еи]фицировать|пройти\s+(вер[еи]фикацию|KYC))|"
    r"не\s+удалось\s+вер[еи]фицировать|"
    r"не\s+удалось\s+подтвердить\s+профиль|"
    r"ошибка\s+при\s+вер[еи]фикации|"
    r"вышла\s+ошибка\s+при\s+вер[еи]фикации|"
    r"вас\s+не\s+удалось\s+вер[еи]фицировать|"
    r"прошу\s+вер[еи]фицировать|"
    r"просьба\s+вер[еи]фицировать|"
    r"вер[еи]фицируйте|"
    r"не\s+проходит\s+вер[еи]фикацию|"
    r"помогите\s+понять.{0,40}пройти\s+KYC|"
    r"не\s+(могу|можем|может)\s+пройти\s+KYC|"
    r"верификацию\s+провести|"
    r"помогите\s+пройти\s+верификацию|"
    r"помоги(те)?\s+с\s+верификац\w*|"
    r"помоги(те)?[\s,!.]+(пожалуйста[\s,!.]+)?верифицир\w*|"
    r"помочь\s+с\s+верификац\w*|"
    r"помощь\s+с\s+верификац\w*|"
    r"нужна\s+помощь\s+с\s+верификац\w*|"
    # «не могу отправить документы и пройти верификацию» — допускаем
    # короткий промежуток между «не могу» и «пройти верификацию».
    r"не\s+(могу|можем|может).{0,40}пройти\s+вер[еи]фикацию|"
    r"не\s+(могу|можем|может)\s+отправить\s+документы"
    r")",
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _build_notification(email: str | None) -> str:
    if email:
        return f"Заказчик сообщил, что исполнитель {email} не может пройти верификацию"
    return "Заказчик сообщил, что исполнитель не может пройти верификацию (email не указан)"


class VerificationScenario(Scenario):
    name = "compliance"

    def detect(self, text: str) -> Detection | None:
        if not _TRIGGER_PATTERN.search(text):
            return None
        email_match = EMAIL_PATTERN.search(text)
        email = email_match.group(0) if email_match else None
        if email:
            return {
                "name": self.name,
                "notification": _build_notification(email),
                "data": {"email": email},
                "needs_followup": False,
            }
        return {
            "name": self.name,
            "notification": _build_notification(None),
            "data": {"email": None},
            "needs_followup": True,
            "followup_hint": "Пожалуйста, укажите email исполнителя для передачи запроса в комплаенс",
        }

    def complete_followup(self, data: dict, followup_text: str) -> Detection | None:
        match = EMAIL_PATTERN.search(followup_text)
        if not match:
            return None
        email = match.group(0)
        return {
            "name": self.name,
            "notification": _build_notification(email),
            "data": {"email": email},
            "needs_followup": False,
        }
