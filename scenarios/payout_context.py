"""Payout context scenario.

Запросы про конкретную выплату или жалобы на проблемы с выплатой.
Три пути:
  1. В сообщении явный ID выплаты         -> SELECT по id
  2. В сообщении email исполнителя        -> SELECT последней выплаты по этому email
  3. Ни ID, ни email                      -> followup: попросить ID или email

Сценарий — informational: его notification = готовый ответ клиенту,
а не уведомление для оператора. ai_response/UI рендерят его как
ответ AI-ассистента.
"""

from __future__ import annotations

import re
from typing import Any

from scenarios.base import Detection, Scenario


_TRIGGER_PATTERN = re.compile(
    r"("
    # «выплата + проблема»
    r"выплат[аеоуыйя]?\s+(не\s+(прошла|пришла|приходит|проходит|дошла|идёт|идет)|"
    r"застряла|зависла|висит|зависает|сломалась|не\s+работает|"
    r"подвис(ла|ли|ает)|обрабатывается|в\s+обработке)|"
    r"не\s+(прошла|приходит|пришла|проходит|могу\s+получить)\s+выплат[аеоуыйя]?|"
    r"подвисл[аи]\s+выплат[аеоуыйя]?|"
    r"проблем[аы]?\s+с\s+выплат[ой]+|"
    r"что\s+с\s+(\w+\s+){0,3}выплат\w*|"
    r"почему\s+(мне\s+)?(не\s+пришла|не\s+приходит|не\s+пришли|не\s+приходят)\s+(выплат|средств|деньг)|"
    r"почему\s+выплат[аы]?\s+не|"
    r"статус\s+выплаты|"
    r"информация\s+по\s+выплате|"
    r"контекст\s+по\s+выплате|"
    r"посмотри(те)?\s+выплат[уыа]|"
    r"проверь(те)?\s+выплат[уыа]|"
    r"глянь(те)?\s+выплат[уыа]|"
    r"можете\s+посмотреть|"
    r"можете\s+проверить\s+выплат|"
    r"по\s+выплате\s+\d+|"
    r"выплата\s+\d+|"
    r"последн\w+\s+выплат\w*|"
    # средства / деньги «висят», «не пришли», «отклонены»
    r"(средства|деньги)\s+(не\s+(пришли|поступили|дошли)|висят\s+в\s+обработке|зависли)|"
    r"висят\s+в\s+обработке|"
    # отклонение платежа
    r"отклонени[яе]\s+(платеж[ау]|выплат[ыу])|"
    r"причин[ауы]\s+отклонени[яе]\s+(платеж|выплат)|"
    r"отклонил[иа]?\s+(платёж|платеж|выплат)|"
    # новые: статусы операций/переводов
    r"статус\s+(операции|операцию|платеж[аи]?|перевод[аеу]?)|"
    # «в каком статусе … выплата»
    r"в\s+каком\s+статусе.{0,40}выплат|"
    # «причину отмен (последних) выплат у исполнителя …»
    r"причин\w*\s+отмен\w*\s+(\w+\s+){0,2}выплат|"
    # «выплата отклонена»
    r"выплат[аы]?\s+отклонен|"
    # «не видит начислений / зачислений / поступлений / средств»
    r"не\s+видит\s+(начислений|зачислений|поступлений|выплат|средств|денег)|"
    # «возврат выплаты», «вернулась ли выплата»
    r"возврат\s+выплат|"
    r"вернул(ась|ся|ись|ось)\s+(ли\s+)?выплат|"
    # «почему нет выплаты»
    r"почему\s+нет\s+выплат|"
    # «задержка выплаты», «выплата задерживается»
    r"задержк[аиу]\s+выплат|"
    r"выплат\w*\s+задерж\w*|"
    # «выплата/платёж висит» (в т.ч. «висит в статусе \"в обработке\"»)
    r"выплат\w*\s+вис(ит|ят|нет|т)|"
    r"платеж\w*\s+вис(ит|ят|нет|т)|"
    r"платёж\s+вис(ит|ят|нет|т)|"
    # самостоятельный сигнал: «висит/вист в статусе/статсе» (с опечатками)
    r"вис(ит|ят|нет|т)\s+в\s+стат\w*|"
    # «почему не прошла одна из сегодняшних выплат», «почему не прошли выплаты»
    r"не\s+прошл[аи]\s+(\w+\s+){0,5}выплат|"
    r"почему\s+не\s+прошл[аи]\s+(\w+\s+){0,5}выплат|"
    # «отправила деньги на карту …» — клиент сообщает о выплате на карту
    r"отправил[аи]?\s+деньги\s+на\s+карту|"
    # «деньги на карту (не) пришли»
    r"на\s+карту.{0,20}(не\s+)?пришли"
    r")",
    re.IGNORECASE,
)

# 5–9 знаков — захватывает payout-ID, не путает с
# годами (2025, 2026) и телефонами (10+).
# Negative lookahead `(?!@)` отсекает локальные части email-адресов
# (например "1234567@domain.tld" — это email, а не payout_id).
_PAYOUT_ID_PATTERN = re.compile(r"\b(\d{5,9})\b(?!@)")
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# task_id в формате «номер задачи 634489», «задача 634489», «task 634489»
_TASK_ID_PATTERN = re.compile(
    r"(?:номер\s+задачи|задач[аеиу]\s*№?|task(?:_id)?\s*#?)\s*[:#]?\s*(\d{4,9})",
    re.IGNORECASE,
)

# Сгруппированные поля payout. Поля, не попавшие ни в одну группу,
# выводятся блоком «Прочее» в конце. Пустые значения скрываются.
_FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Идентификаторы", (
        "id", "mirror_id", "original_id", "contractor_id", "company_id",
        "gateway_pricing_id", "staff_id", "provider_id", "final_provider_id",
    )),
    ("Статус и ошибки", (
        "status", "error_code", "cancel_reason", "first_moderation_reason",
        "final_provider_context_error", "last_webhook_error",
    )),
    ("Сумма и налоги", (
        "amount", "currency", "amount_result", "final_currency",
        "amount_result_in_final_currency",
        "amount_result_in_final_currency_after_tax",
        "amount_result_in_rub_after_tax", "amount_result_in_usd_after_tax",
        "amount_in_rub_cbr",
        "payout_commission", "payout_commission_in_rub_cbr",
        "payout_cost_in_rub_cbr",
        "tax_amount", "tax_currency", "tax_rate", "tax_status",
        "gateway_conversion_cost_in_rub",
        "conversion_commission_in_rub",
        "conversion_commission_in_final_currency",
    )),
    ("Конверсия", (
        "effective_conversion_rate", "market_conversion_rate",
        "final_currency_to_rub_rate_cbr", "payout_currency_to_rub_rate_cbr",
        "provider_fixed_commission_to_rub_rate",
    )),
    ("Исполнитель", (
        "contractor_email", "contractor_first_name", "contractor_father_name",
        "contractor_last_name", "contractor_country", "contractor_country_name",
        "contractor_inn", "kyc_level",
    )),
    ("Карта", (
        "card_number", "card_bank", "card_country", "card_system",
        "card_bin", "card_mc_region", "card_visa_region", "card_token",
    )),
    ("Заказчик", (
        "company_name", "company_country", "company_country_name",
        "company_type", "company_account_currency", "company_payin_currency",
        "company_holds_income_tax",
    )),
    ("Провайдер", (
        "provider", "final_provider", "gateway", "request_ids",
    )),
    ("Платформа", (
        "platform", "final_platform", "payout_area", "payout_type",
    )),
    ("Mirror (cross-border)", (
        "mirror_task_id", "mirror_platform", "mirror_provider", "mirror_instance",
    )),
    ("Задача", (
        "last_task_id", "last_task_name", "last_task_description",
        "last_task_service_label", "last_task_category_label",
        "last_task_company_mt_legal_entity", "last_task_company_name",
        "last_task_service_id", "last_task_category_id", "last_task_company_id",
    )),
    ("Модерация", (
        "need_moderation", "passed_moderation",
    )),
    ("Менеджмент", (
        "counts_to_dept", "account_manager", "sales_manager",
    )),
    ("Время", (
        "created_at", "completed_at",
        "processing_time_in_days", "processing_time_in_working_days",
        "bank_response_time_hours",
        "completed_day_of_month", "completed_month_start",
        "completed_year_quarter",
    )),
    ("Документы и метки", (
        "receipt_link", "not_rosburn_efficient",
    )),
)

# Максимальная длина значения в выводе — длинные токены/описания обрезаем.
_VALUE_TRUNCATE = 250


def _fmt(value: Any) -> str:
    if value is None or value == "" or value == "null":
        return "—"
    return str(value)


def _format_value(v: Any) -> str:
    s = str(v)
    if len(s) > _VALUE_TRUNCATE:
        s = s[: _VALUE_TRUNCATE] + "…"
    return s


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == "null"


def _format_payout(row: dict[str, Any]) -> str:
    """Компактный вывод payout: только ключевые поля для оператора.

    Базовый набор (всегда, если непусто): id, status, amount+currency,
    contractor_email, card_bank + card_number, created_at, completed_at.
    Условно: first_moderation_reason / error_code (если статус canceled).
    """
    pid = row.get("id")
    status = row.get("status") or ""
    amount = row.get("amount")
    currency = row.get("currency") or ""

    pairs: list[tuple[str, str]] = []
    pairs.append(("status", _fmt(status)))
    if amount is not None and amount != "":
        pairs.append(("amount", f"{amount} {currency}".strip()))
    if not _is_empty(row.get("contractor_email")):
        pairs.append(("contractor_email", _fmt(row.get("contractor_email"))))
    card_bank = row.get("card_bank")
    card_number = row.get("card_number")
    if not _is_empty(card_bank) or not _is_empty(card_number):
        card_line = " ".join(
            x for x in [_fmt(card_bank) if not _is_empty(card_bank) else "",
                        _fmt(card_number) if not _is_empty(card_number) else ""]
            if x
        )
        pairs.append(("card", card_line))
    if not _is_empty(row.get("created_at")):
        pairs.append(("created_at", _fmt(row.get("created_at"))))
    if not _is_empty(row.get("completed_at")):
        pairs.append(("completed_at", _fmt(row.get("completed_at"))))
    provider = row.get("provider") or row.get("final_provider")
    if not _is_empty(provider):
        pairs.append(("provider", _fmt(provider)))
    if not _is_empty(row.get("company_name")):
        pairs.append(("company_name", _fmt(row.get("company_name"))))
    if status == "canceled":
        if not _is_empty(row.get("first_moderation_reason")):
            pairs.append(("reason", _fmt(row.get("first_moderation_reason"))))
        elif not _is_empty(row.get("error_code")):
            pairs.append(("error_code", _fmt(row.get("error_code"))))

    max_w = max(len(k) for k, _ in pairs)
    body = "\n".join(f"  {k.ljust(max_w)} : {_format_value(v)}" for k, v in pairs)
    return f"📋 *Контекст по выплате #{pid}*\n```\n{body}\n```"


def _extract_task_id(text: str) -> int | None:
    """Сначала ищем явное упоминание задачи — это приоритетнее, чем payout_id."""
    match = _TASK_ID_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _extract_payout_id(text: str, *, exclude: set[int] | None = None) -> int | None:
    exclude = exclude or set()
    for m in _PAYOUT_ID_PATTERN.finditer(text):
        val = int(m.group(1))
        if val not in exclude:
            return val
    return None


def _fetch_by_id(payout_id: int) -> dict | None:
    from payouts_agent import run_sql
    sql = (
        f"SELECT * FROM t_payout_extended "
        f"WHERE id = {int(payout_id)} LIMIT 1"
    )
    rows = run_sql(sql)
    return rows[0] if rows else None


def _fetch_latest_by_task_id(task_id: int) -> dict | None:
    """Ищем последнюю выплату по задаче.

    Задача может быть связана с выплатой двумя способами:
      - last_task_id   — выплата объединяет несколько задач, эта — последняя
      - mirror_task_id — выплата создана напрямую под конкретную задачу
    Берём самую свежую запись из тех, что нашлись хотя бы по одному полю.
    """
    from payouts_agent import run_sql
    tid = int(task_id)
    sql = (
        f"SELECT * FROM t_payout_extended "
        f"WHERE last_task_id = {tid} OR mirror_task_id = {tid} "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    rows = run_sql(sql)
    return rows[0] if rows else None


def _fetch_latest_by_email(email: str) -> dict | None:
    from payouts_agent import run_sql
    safe = email.replace("'", "''")
    sql = (
        f"SELECT * FROM t_payout_extended "
        f"WHERE contractor_email = '{safe}' "
        f"ORDER BY created_at DESC LIMIT 1"
    )
    rows = run_sql(sql)
    return rows[0] if rows else None


class PayoutContextScenario(Scenario):
    name = "payout_context"

    def detect(self, text: str) -> Detection | None:
        has_trigger = bool(_TRIGGER_PATTERN.search(text))
        if not has_trigger:
            return None
        return self._resolve(text)

    def complete_followup(self, data: dict, followup_text: str) -> Detection | None:
        # Followup может быть произвольной короткой строкой — пробуем по любому
        # из идентификаторов; триггер не требуется.
        result = self._resolve(followup_text)
        # Если ничего не извлекли — пусть scенарий продолжает ждать
        if result is None or result.get("needs_followup"):
            return None
        return result

    def _resolve(self, text: str) -> Detection | None:
        # 1) приоритет — явный «номер задачи N»
        task_id = _extract_task_id(text)
        if task_id is not None:
            return self._build_for_task(task_id)

        # 2) явный payout_id
        payout_id = _extract_payout_id(text)
        if payout_id is not None:
            return self._build_for_id(payout_id)

        # 3) email
        email_match = _EMAIL_PATTERN.search(text)
        if email_match:
            return self._build_for_email(email_match.group(0))

        # 4) ничего не нашли — followup
        return {
            "name": self.name,
            "notification": "",
            "data": {},
            "needs_followup": True,
            "followup_hint": (
                "Чтобы найти контекст по выплате — пришлите, пожалуйста, "
                "ID выплаты, номер задачи или email исполнителя."
            ),
            "informational": True,
        }

    def _build_for_id(self, payout_id: int) -> Detection:
        try:
            row = _fetch_by_id(payout_id)
        except Exception as exc:
            return {
                "name": self.name,
                "notification": (
                    f"⚠ Не удалось получить выплату #{payout_id} из БД: {exc}"
                ),
                "data": {"payout_id": payout_id, "error": str(exc)},
                "needs_followup": False,
                "informational": True,
            }
        if not row:
            return {
                "name": self.name,
                "notification": f"Выплата #{payout_id} не найдена в базе.",
                "data": {"payout_id": payout_id},
                "needs_followup": False,
                "informational": True,
            }
        return {
            "name": self.name,
            "notification": _format_payout(row),
            "data": {"payout_id": payout_id, "row": row},
            "needs_followup": False,
            "informational": True,
        }

    def _build_for_task(self, task_id: int) -> Detection:
        try:
            row = _fetch_latest_by_task_id(task_id)
        except Exception as exc:
            return {
                "name": self.name,
                "notification": (
                    f"⚠ Не удалось найти выплату по задаче #{task_id}: {exc}"
                ),
                "data": {"task_id": task_id, "error": str(exc)},
                "needs_followup": False,
                "informational": True,
            }
        if not row:
            return {
                "name": self.name,
                "notification": f"По задаче #{task_id} выплат не найдено.",
                "data": {"task_id": task_id},
                "needs_followup": False,
                "informational": True,
            }
        return {
            "name": self.name,
            "notification": (
                f"Выплата по задаче #{task_id}:\n\n" + _format_payout(row)
            ),
            "data": {"task_id": task_id, "row": row},
            "needs_followup": False,
            "informational": True,
        }

    def _build_for_email(self, email: str) -> Detection:
        try:
            row = _fetch_latest_by_email(email)
        except Exception as exc:
            return {
                "name": self.name,
                "notification": (
                    f"⚠ Не удалось найти выплаты по {email}: {exc}"
                ),
                "data": {"email": email, "error": str(exc)},
                "needs_followup": False,
                "informational": True,
            }
        if not row:
            return {
                "name": self.name,
                "notification": f"По исполнителю {email} выплат не найдено.",
                "data": {"email": email},
                "needs_followup": False,
                "informational": True,
            }
        return {
            "name": self.name,
            "notification": (
                f"Последняя выплата исполнителя {email}:\n\n"
                + _format_payout(row)
            ),
            "data": {"email": email, "row": row},
            "needs_followup": False,
            "informational": True,
        }
