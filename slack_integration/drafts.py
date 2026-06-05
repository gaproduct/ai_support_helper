"""
Дублирование сообщений сценариев в отдельный канал #support_scenarios_draft.

Поток:
  1) сценарий compliance/finance/accounting запускается → пост в обычный канал
  2) сразу публикуем копию в drafts-канал (если он настроен)
  3) сохраняем связку в таблицу scenario_drafts
  4) в обычный тред кладём permalink на пост в drafts-канале
"""

import logging
import re
from datetime import datetime, timedelta

from ai.draft import generate_draft
from ai.client_name import extract_client_name
from core.config import settings
from core.database import IncomingMessage, ScenarioDraft, get_session
from slack_integration.client import build_permalink, post_slack


def _normalize_channel_name(name: str) -> str:
    """Same normalization as webhooks.flomni — sorted alnum tokens, lowercased."""
    if not name:
        return ""
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", name.lower())
    return " ".join(sorted(tokens))

log = logging.getLogger(__name__)

# Сценарии, для которых дублируем в drafts-канал.
DRAFT_SCENARIOS: set[str] = {"compliance", "finance", "accounting", "payout_context"}

# Сценарии, для которых сразу автогенерим первый черновик ответа клиенту
# (для остальных бот ждёт @mention оператора в drafts-треде).
AUTO_DRAFT_SCENARIOS: set[str] = {"payout_context"}


def build_draft_blocks(draft_text: str, draft_id: int) -> list[dict]:
    """Block Kit с текстом черновика и кнопками действий."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "✏️ *Черновик ответа клиенту:*\n" + f"```{draft_text}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Отправить сообщение"},
                    "action_id": "send_draft",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔄 Перегенерировать"},
                    "action_id": "regenerate_draft",
                    "value": str(draft_id),
                },
            ],
        },
    ]

_SCENARIO_TITLES: dict[str, str] = {
    "compliance": "Верификация",
    "finance": "Пополнение баланса",
    "accounting": "Запрос документов",
    "payout_context": "Контекст по выплате",
}


def duplicate_to_drafts(
    scenario_name: str,
    notification: str,
    original_thread_ts: str | None,
    client_id: str | None,
) -> None:
    """
    Публикует копию уведомления сценария в drafts-канал, сохраняет связку
    в БД и кладёт permalink обратно в исходный тред.
    """
    if scenario_name not in DRAFT_SCENARIOS:
        return
    if not settings.slack_drafts_channel_id:
        log.info("Drafts channel not configured, skipping duplication.")
        return

    title = _SCENARIO_TITLES.get(scenario_name, scenario_name)

    # Достаём имя клиента из IncomingMessage.name (вырезаем "MadeTask").
    # Если IncomingMessage не нашли — постить в drafts нет смысла: при
    # @mention в треде мы не сможем восстановить контекст обращения.
    client_label = ""
    incoming_row: IncomingMessage | None = None
    if client_id:
        with get_session() as db:
            incoming_row = (
                db.query(IncomingMessage)
                .filter(
                    IncomingMessage.client_id == client_id,
                    IncomingMessage.done.is_(False),
                )
                .first()
            )
            if incoming_row:
                client_label = extract_client_name(incoming_row.name)

                # Анти-дубль: если по этому IncomingMessage уже постили draft
                # для этого сценария в последние 30 минут — пропускаем.
                # Защищает от TG-twin (Flomni шлёт один и тот же текст через
                # два коннектора → сценарий триггерится дважды).
                recent_cutoff = datetime.utcnow() - timedelta(minutes=30)
                existing_draft = (
                    db.query(ScenarioDraft)
                    .filter(
                        ScenarioDraft.incoming_message_id == incoming_row.id,
                        ScenarioDraft.scenario_name == scenario_name,
                        ScenarioDraft.created_at >= recent_cutoff,
                    )
                    .first()
                )
                if existing_draft is not None:
                    log.info(
                        "Skipping duplicate draft post: incoming=%s scenario=%s "
                        "existing draft_ts=%s",
                        incoming_row.id, scenario_name, existing_draft.draft_thread_ts,
                    )
                    return

                # Cross-incoming TG-twin dedup: тот же сценарий мог уже улететь
                # для соседнего IncomingMessage (другой client_id, но тот же
                # нормализованный name — например, два коннектора одной TG-группы).
                # Расширенное окно 24h, т.к. близнецы могут жить несколько суток.
                normalized_name = _normalize_channel_name(incoming_row.name or "")
                if normalized_name:
                    wide_cutoff = datetime.utcnow() - timedelta(hours=24)
                    candidates = (
                        db.query(ScenarioDraft, IncomingMessage)
                        .join(
                            IncomingMessage,
                            ScenarioDraft.incoming_message_id == IncomingMessage.id,
                        )
                        .filter(
                            ScenarioDraft.scenario_name == scenario_name,
                            ScenarioDraft.created_at >= wide_cutoff,
                            ScenarioDraft.incoming_message_id != incoming_row.id,
                        )
                        .all()
                    )
                    twin_draft = next(
                        (
                            sd for sd, im in candidates
                            if _normalize_channel_name(im.name or "") == normalized_name
                        ),
                        None,
                    )
                    if twin_draft is not None:
                        log.info(
                            "Skipping TG-twin duplicate draft: incoming=%s scenario=%s "
                            "twin_incoming=%s twin_draft_ts=%s",
                            incoming_row.id, scenario_name,
                            twin_draft.incoming_message_id, twin_draft.draft_thread_ts,
                        )
                        return

    if incoming_row is None:
        log.info(
            "No IncomingMessage found for client_id=%s — skipping drafts post "
            "(no anchor for future @mentions).",
            client_id,
        )
        return

    client_line = f"*Клиент:* {client_label}\n" if client_label else ""

    # Кратко передаём суть исходного вопроса + permalink на исходный тред —
    # чтобы оператор в drafts-канале мог одним кликом провалиться в первоисточник.
    brief_raw = (incoming_row.first_message_text or "").strip().replace("\n", " ")
    brief = brief_raw[:160] + "…" if len(brief_raw) > 160 else brief_raw
    original_link_line = ""
    if original_thread_ts:
        original_permalink = build_permalink(settings.slack_channel_id, original_thread_ts)
        if original_permalink:
            if brief:
                original_link_line = (
                    f"*Исходный вопрос:* <{original_permalink}|открыть тред> — «{brief}»\n"
                )
            else:
                original_link_line = (
                    f"*Исходный вопрос:* <{original_permalink}|открыть тред>\n"
                )

    draft_text = (
        f"🔄 *{title}*\n"
        f"{client_line}"
        f"{original_link_line}"
        f"{notification}\n\n"
        f"_Упомяните бота в этом треде, чтобы получить черновик ответа клиенту._"
    )
    draft_ts = post_slack(draft_text, channel=settings.slack_drafts_channel_id)
    if not draft_ts:
        log.warning("Failed to post draft duplicate for scenario=%s", scenario_name)
        return

    # Сохраняем связку (incoming_row уже найден выше — на этой стадии гарантирован)
    saved_draft_id: int | None = None
    client_first_message: str = ""
    with get_session() as db:
        row = (
            db.query(IncomingMessage)
            .filter(IncomingMessage.id == incoming_row.id)
            .first()
        )
        if row:
            new_draft = ScenarioDraft(
                incoming_message_id=row.id,
                scenario_name=scenario_name,
                original_channel=settings.slack_channel_id,
                original_thread_ts=original_thread_ts,
                draft_channel=settings.slack_drafts_channel_id,
                draft_thread_ts=draft_ts,
                notification_text=notification,
            )
            db.add(new_draft)
            db.commit()
            db.refresh(new_draft)
            saved_draft_id = new_draft.id
            client_first_message = row.first_message_text or ""
            log.info(
                "ScenarioDraft saved: incoming=%s scenario=%s draft_ts=%s",
                row.id, scenario_name, draft_ts,
            )

    # Авто-генерация первого черновика для сценариев из AUTO_DRAFT_SCENARIOS
    # (например, payout_context — у нас уже есть весь контекст из БД, можно
    # сразу предложить оператору готовый ответ клиенту).
    if saved_draft_id is not None and scenario_name in AUTO_DRAFT_SCENARIOS:
        draft_text = generate_draft(
            scenario_name=scenario_name,
            scenario_notification=notification,
            client_message=client_first_message,
            thread_history=[],
            operator_instruction="",
            client_name=client_label,
        )
        if draft_text:
            blocks = build_draft_blocks(draft_text, saved_draft_id)
            # 1) Постим черновик в drafts-канал (под root-уведомлением)
            active_ts = post_slack(
                text="Черновик ответа клиенту готов.",
                channel=settings.slack_drafts_channel_id,
                thread_ts=draft_ts,
                blocks=blocks,
            )
            # 2) Зеркалим в исходный тред основного канала — чтобы оператору
            #    не нужно было прыгать в drafts-канал.
            mirror_ts = None
            if original_thread_ts:
                mirror_ts = post_slack(
                    text="Черновик ответа клиенту готов.",
                    channel=settings.slack_channel_id,
                    thread_ts=original_thread_ts,
                    blocks=blocks,
                )
            # 3) Запоминаем ts обеих копий, чтобы при regenerate обновить обе.
            with get_session() as db:
                d = db.query(ScenarioDraft).filter(ScenarioDraft.id == saved_draft_id).first()
                if d:
                    d.active_draft_msg_ts = active_ts
                    d.mirror_channel = settings.slack_channel_id if mirror_ts else None
                    d.mirror_msg_ts = mirror_ts
                    db.commit()
        else:
            log.warning(
                "Auto-draft generation failed for scenario=%s draft_id=%s",
                scenario_name, saved_draft_id,
            )

    # Кросс-ссылка обратно в обычный тред
    if original_thread_ts:
        permalink = build_permalink(settings.slack_drafts_channel_id, draft_ts)
        if permalink:
            post_slack(
                f"🧵 Скопировано в <{permalink}|канал черновиков> — "
                f"оператор может тегнуть бота там для подготовки ответа.",
                thread_ts=original_thread_ts,
            )
