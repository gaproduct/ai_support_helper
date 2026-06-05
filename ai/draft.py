"""
Генерация AI-черновика ответа клиенту для ChatOps в #support_scenarios_draft.

Контекст для модели:
  - название сценария (compliance / finance / accounting)
  - текст уведомления, которое запустило сценарий
  - история обращения клиента (аккумулированный first_message_text)
  - история обсуждения в draft-треде (комментарии операторов)
  - конкретное указание оператора (текст mention'а)
"""

import logging

from openai import OpenAI

from core.config import settings

log = logging.getLogger(__name__)

_client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

SYSTEM_PROMPT = (
    "Ты — сотрудник службы поддержки платформы MadeTask. "
    "Тебе нужно подготовить ответ клиенту по его обращению.\n\n"
    "Правила:\n"
    "  • пиши кратко, по делу, без воды и приветствий типа «Здравствуйте!» "
    "(приветствие уже было сказано выше в переписке);\n"
    "  • тон — вежливый, профессиональный, на «вы»;\n"
    "  • не выдумывай факты, которых нет в контексте;\n"
    "  • если контекста не хватает — задай уточняющий вопрос клиенту;\n"
    "  • используй указание оператора как основу — он лучше знает суть.\n\n"
    "Верни ТОЛЬКО сам текст ответа клиенту, без префиксов, без markdown-обёрток."
)

# Жёсткие правила, специфичные сценарию. Подмешиваются в system-роль —
# модели сильнее следуют system, чем user. Сюда кладём ТОЛЬКО правила
# («как отвечать», «что нельзя говорить»), без data-glossary (он в _SCENARIO_HINTS).
_SCENARIO_SYSTEM_RULES: dict[str, str] = {
    "payout_context": (
        "СЦЕНАРИЙ: контекст по выплате исполнителя.\n"
        "ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА (нарушение = брак):\n"
        "  1) НИКОГДА не упоминай в ответе клиенту: id выплаты, contractor_id, "
        "card_token, card_bin, mirror_id, original_id, provider_id, "
        "gateway_pricing_id, staff_id, request_ids, internal-поля.\n"
        "  2) Если status = completed — ОБЯЗАТЕЛЬНО:\n"
        "       — поздравь короткой фразой («выплата успешно прошла» / "
        "«деньги отправлены»);\n"
        "       — укажи дату и время завершения в формате «8 мая в 13:12» "
        "(берём из completed_at);\n"
        "       — укажи банк-эмитент (card_bank) и последние 4 цифры карты "
        "(берём из card_number, формат «****0745»).\n"
        "  3) Если status = canceled — объясни причину человеческим языком "
        "из first_moderation_reason; скажи, что нужно сделать дальше.\n"
        "  4) Если status = in_process — скажи, что выплата в обработке, "
        "укажи дату создания (created_at) в формате «8 мая в 13:09», "
        "попроси немного подождать.\n"
        "  5) Не вставляй технические значения (smart_glocal, ES_SMGL_RUB, "
        "ps_3337348171 и т.п.) — они для оператора, не для клиента."
    ),
}

_SCENARIO_LABELS: dict[str, str] = {
    "compliance": "Верификация исполнителя",
    "finance": "Пополнение баланса",
    "accounting": "Запрос документов в бухгалтерию",
    "payout_context": "Контекст по выплате",
}

# Семантика полей сценария — это ДАННЫЕ (что значит каждое поле),
# поэтому остаётся в user-промпте рядом с самим контекстом.
# Правила «как отвечать» — в _SCENARIO_SYSTEM_RULES выше.
_SCENARIO_HINTS: dict[str, str] = {
    "payout_context": (
        "Семантика ключевых полей контекста выплаты:\n"
        "  • status — статус выплаты (canceled / in_process / completed).\n"
        "  • error_code — техническая причина отказа (incorrect_full_name = "
        "некорректное ФИО, declined_by_bank = отказ банка и т.п.).\n"
        "  • first_moderation_reason — причина модерации на русском.\n"
        "  • card_number — маскированный номер карты (последние 4 цифры).\n"
        "  • card_bank — банк-эмитент карты.\n"
        "  • card_country / card_system — страна и платёжная система карты.\n"
        "  • created_at — когда выплата создана.\n"
        "  • completed_at — когда выплата завершилась.\n"
        "  • processing_time_in_days / _in_working_days — сколько шла."
    ),
}


def generate_draft(
    scenario_name: str,
    scenario_notification: str,
    client_message: str,
    thread_history: list[str],
    operator_instruction: str,
    client_name: str = "",
) -> str | None:
    """
    Сформировать черновик ответа клиенту.
    Возвращает текст черновика или None при ошибке/недоступном OpenAI.
    """
    if _client is None:
        log.warning("OpenAI client not configured — cannot generate draft.")
        return None

    # Режим «свободный ответ» — когда оператор жмёт кнопку «Сгенерировать ответ»
    # в обычном треде без активного сценария. Опускаем scenario-блоки и метки,
    # чтобы модель не цеплялась за пустые поля.
    is_generic = not scenario_name

    history_block = "\n".join(f"- {h}" for h in thread_history) if thread_history else "(пусто)"
    op_text = operator_instruction or (
        "(оператор не дал явного указания, сформулируй уместный ответ по контексту)"
    )
    client_block = f"Клиент: {client_name}\n" if client_name else ""

    if is_generic:
        scenario_block = ""
        hint_block = ""
        context_block = ""
        system_content = SYSTEM_PROMPT
    else:
        label = _SCENARIO_LABELS.get(scenario_name, scenario_name)
        scenario_block = f"Сценарий: {label}\n"
        hint = _SCENARIO_HINTS.get(scenario_name, "")
        hint_block = f"Семантика полей контекста:\n{hint}\n\n" if hint else ""
        context_block = (
            f"Контекст сценария:\n{scenario_notification or '(не указано)'}\n\n"
        )
        scenario_rules = _SCENARIO_SYSTEM_RULES.get(scenario_name, "")
        system_content = SYSTEM_PROMPT + ("\n\n" + scenario_rules if scenario_rules else "")

    user_prompt = (
        f"{scenario_block}"
        f"{client_block}"
        f"{hint_block}"
        f"{context_block}"
        f"Исходное обращение клиента:\n«{client_message or '(не указано)'}»\n\n"
        f"История обсуждения в чате операторов:\n{history_block}\n\n"
        f"Указание оператора:\n«{op_text}»\n\n"
        f"Подготовь текст ответа клиенту."
    )

    try:
        response = _client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception as exc:
        log.error("OpenAI draft generation error: %s", exc)
        return None
