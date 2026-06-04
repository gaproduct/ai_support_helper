"""
AI-response orchestration for Slack threads.

Priority:
  0. Pending followup (compliance/payout_context ждёт уточнение)
                                → resolve_followup, post result
  1. Greeting / empty           → silent wait
  2. Scenario triggered         → run scenario, post notification (+save pending)
  3. KB search (similarity ≥ KB_THRESHOLD) → post answer
  4. KB miss                    → post «не смог подобрать ответ»
"""

import json
import logging

import auto_response
import scenarios
from database import IncomingMessage, get_session
from slack_client import post_slack
from slack_drafts import duplicate_to_drafts

log = logging.getLogger(__name__)

# Прод-порог схожести для KB-автоответа: чем выше, тем строже отбираются
# совпадения. 0.8 = только реально близкие к статье вопросы.
KB_THRESHOLD = 0.8

# Human-readable scenario titles for Slack messages.
_SCENARIO_TITLES: dict[str, str] = {
    "compliance": "Отправка на верификацию",
    "finance": "Отправка запроса в финансовый чат",
    "accounting": "Отправка запроса в бухгалтерию",
    "payout_context": "Контекст по выплате",
}


def _scenario_title(name: str) -> str:
    return _SCENARIO_TITLES.get(name, name)


def _post_detection(
    detection: dict,
    thread_ts: str | None,
    client_id: str | None = None,
) -> None:
    if detection.get("needs_followup"):
        post_slack(
            f"🤖 *AI-ассистент:* {detection.get('followup_hint', '')}",
            thread_ts=thread_ts,
        )
    elif detection.get("informational"):
        post_slack(
            f"🤖 *AI-ассистент:*\n{detection['notification']}",
            thread_ts=thread_ts,
        )
        # Информационные сценарии (payout_context) тоже дублируем в drafts —
        # там бот сразу автогенерит черновик ответа клиенту.
        duplicate_to_drafts(
            scenario_name=detection["name"],
            notification=detection.get("notification", ""),
            original_thread_ts=thread_ts,
            client_id=client_id,
        )
    else:
        post_slack(
            f"🔄 *Запущен сценарий:* {_scenario_title(detection['name'])}\n"
            f"💬 {detection['notification']}",
            thread_ts=thread_ts,
        )
        # Дублируем «активные» сценарии (compliance/finance/accounting)
        # в канал #support_scenarios_draft для ChatOps-обработки.
        duplicate_to_drafts(
            scenario_name=detection["name"],
            notification=detection.get("notification", ""),
            original_thread_ts=thread_ts,
            client_id=client_id,
        )


def post_ai_response(
    message_text: str,
    thread_ts: str | None,
    client_id: str | None = None,
) -> None:
    if not message_text or not message_text.strip():
        return

    # 0. Pending followup
    if client_id:
        with get_session() as db:
            row: IncomingMessage | None = (
                db.query(IncomingMessage)
                .filter(
                    IncomingMessage.client_id == client_id,
                    IncomingMessage.done.is_(False),
                )
                .first()
            )
            if row and row.pending_scenario:
                scenario = scenarios.get_scenario(row.pending_scenario)
                if scenario:
                    try:
                        data = json.loads(row.pending_data or "{}")
                    except Exception:
                        data = {}
                    completed = scenario.complete_followup(data, message_text)
                    if completed:
                        # followup закрыт
                        row.pending_scenario = None
                        row.pending_data = None
                        db.commit()
                        _post_detection(completed, thread_ts, client_id=client_id)
                        return
                    # Не смогли извлечь нужное — оставляем pending как есть,
                    # ничего не постим (повторно не спамим).
                    return

    # 1. Greeting — wait for the actual question
    if auto_response._is_greeting(message_text):
        return

    # 2. Scenario detection
    detection = scenarios.detect_scenario(message_text)
    if detection:
        # Если сценарий запросил followup — сохраняем состояние
        if detection.get("needs_followup") and client_id:
            with get_session() as db:
                row = (
                    db.query(IncomingMessage)
                    .filter(
                        IncomingMessage.client_id == client_id,
                        IncomingMessage.done.is_(False),
                    )
                    .first()
                )
                if row:
                    row.pending_scenario = detection["name"]
                    row.pending_data = json.dumps(
                        detection.get("data", {}), ensure_ascii=False
                    )
                    db.commit()
                    log.info(
                        "Pending followup saved: client=%s scenario=%s",
                        client_id, detection["name"],
                    )
        _post_detection(detection, thread_ts, client_id=client_id)
        return

    # 3-4. KB search (порог 0.8)
    kb = auto_response.search(message_text, threshold=KB_THRESHOLD)
    article = kb.get("article")
    if article:
        question = article.get("question", "")
        answer = article.get("answer", "")
        similarity = article.get("similarity", 0)
        post_slack(
            f"🤖 *AI-ассистент* (схожесть {float(similarity):.2f})\n"
            f"_{question}_\n\n{answer}",
            thread_ts=thread_ts,
        )
        return

    # KB miss — тишина (раньше постили "не смог подобрать ответ", убрано,
    # т.к. сообщение появлялось почти на каждое обращение и засоряло треды).
    return
