"""
Database models and connection setup.
PostgreSQL via SQLAlchemy (sync, psycopg2).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship

from config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class IncomingMessage(Base):
    """
    Tracks incoming Flomni clients that need message history fetched.
    Analogous to Google Sheets sheet "Входящие сообщения".
    """
    __tablename__ = "incoming_messages"

    id = Column(Integer, primary_key=True)
    client_id = Column(String(255), nullable=False, index=True)  # receiver ID
    name = Column(String(512))
    # Email клиента из metaData вебхука (у виджета и ЛК profile приходит пустым).
    client_email = Column(String(320))
    first_message_text = Column(Text)       # accumulated message text (all messages joined)
    first_message_at = Column(String(64))   # kept as string to match Flomni's format
    last_message_at = Column(String(64))
    done = Column(Boolean, default=False, nullable=False, index=True)
    auto_response_sent = Column(Boolean, default=False, nullable=False)  # KB suggestion fired
    slack_thread_ts = Column(String(64))   # ts of the root Slack message (thread anchor)
    # Сценарий, ожидающий уточнение от клиента (compliance ждёт email,
    # payout_context ждёт ID/email/номер задачи). Сбрасывается при completion.
    pending_scenario = Column(String(64))
    pending_data = Column(Text)            # JSON с данными для complete_followup
    # Источник обращения: 'flomni' (по умолчанию для legacy) | 'chatapp' | ...
    source = Column(String(32), nullable=False, default="flomni", index=True)
    # ChatApp-only routing (NULL для Flomni)
    license_id = Column(String(64))
    messenger_type = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Dialog(Base):
    """
    Accumulated conversation from any channel (Flomni, Telegram, Gmail, ChatApp).
    Analogous to Google Sheets sheet "История диалогов".

    ChatApp-specific columns (license_id, messenger_type, dialog_date, chat_name,
    phone, email, messages_count, messages_json) NULL для остальных источников.
    Уникальность для ChatApp обеспечивается partial UNIQUE индексом
    uq_dialogs_chatapp_chat_day (управляется напрямую в БД).
    """
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True)
    # Unique identifier per channel: receiver_id / telegram chat_id / gmail thread_id / chatapp chat_id
    client_id = Column(String(512), nullable=False, index=True)
    source = Column(String(32), nullable=False)  # "flomni" | "telegram" | "gmail" | "chatapp"
    messages_text = Column(Text, default="")     # accumulated conversation text (flat)
    messages_json = Column(Text)                  # JSON-массив сообщений (только ChatApp)
    started_at = Column(String(64))
    finished_at = Column(String(64))
    processed = Column(Boolean, default=False, nullable=False, index=True)  # AI analysis done
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ChatApp-only поля (NULL для остальных источников)
    license_id = Column(String(64))
    messenger_type = Column(String(32))
    dialog_date = Column(Date, index=True)
    chat_name = Column(String(512))
    phone = Column(String(64))
    email = Column(String(255))                  # email из контакта ChatApp (chat/fromUser)
    messages_count = Column(Integer)
    # Email исполнителя, вычлененный из текста диалога (для атрибуции к компании).
    # Заполняется офлайн-джобом extract_dialog_emails.py и онлайн при инсёрте.
    executor_email = Column(String(255), index=True)
    # Компания исполнителя — резолвится из executor_email через Superset
    # (mv.t_contractor_extended.primary_active_company_name). Заполняется джобом
    # resolve_dialog_companies.py. Внутренние компании (Apzone/Rosburn/Efficient)
    # отфильтрованы blacklist'ом.
    company = Column(String(512), index=True)

    analysis = relationship("AnalysisResult", back_populates="dialog", uselist=False)


class AnalysisResult(Base):
    """
    AI-generated analysis for a dialog. Унифицированная классификация по
    методологии май-отчёта: одна категория из канонического списка плюс
    сторона (customer / executor). Списки категорий и системный промпт
    лежат в ai_analysis.py.

    Fields:
      summary     — краткое содержание диалога (1-3 предложения)
      category    — основная категория из канонического списка (CATEGORIES_CUST /
                    CATEGORIES_EXEC, см. ai_analysis.py). «Потенциальный клиент»
                    — один из вариантов.
      subcategory — уточнение внутри category для категорий с детализацией
                    (SUBCATEGORIES в ai_analysis.py). NULL, если у категории
                    нет подкатегорий или модель не смогла уточнить.
      side        — customer | executor (в legacy-строках до миграции 004 может
                    содержать историческую подкатегорию, не использовать для
                    аналитики без фильтра по created_at)
      sentiment   — positive / neutral / negative
      priority    — low / medium / high / critical
      resolution  — resolved / unresolved / escalated
      rationale   — обоснование выбора side с прямой цитатой из переписки
                    (зачем: якорит решение модели в реальном тексте,
                    повышает точность customer/executor на edge-кейсах)
      raw_response — полный JSON-ответ модели (для отладки)
    """
    __tablename__ = "analysis_results"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=False)
    summary = Column(Text)
    category = Column(String(256))
    subcategory = Column(String(256))  # уточнение внутри category (см. SUBCATEGORIES в ai_analysis.py)
    side = Column(String(256))       # customer | executor
    sentiment = Column(String(32))   # positive | neutral | negative
    priority = Column(String(32))    # low | medium | high | critical
    resolution = Column(String(32))  # resolved | unresolved | escalated
    rationale = Column(Text)         # обоснование side с цитатой
    raw_response = Column(Text)      # full JSON from model
    created_at = Column(DateTime, default=datetime.utcnow)

    dialog = relationship("Dialog", back_populates="analysis")


class ScenarioDraft(Base):
    """
    Связка обычного Slack-треда обращения и поста-копии в #support_scenarios_draft.
    Используется чтобы при @mention в draft-треде понять, к какому клиенту/обращению
    относится тред, и сформировать AI-черновик ответа.
    """
    __tablename__ = "scenario_drafts"

    id = Column(Integer, primary_key=True)
    incoming_message_id = Column(Integer, ForeignKey("incoming_messages.id"), nullable=False, index=True)
    scenario_name = Column(String(64), nullable=False)
    # Где висит оригинальный пост сценария (обычный канал)
    original_channel = Column(String(64))
    original_thread_ts = Column(String(64))
    # Где висит копия в draft-канале — это якорь, по которому ловим mention
    draft_channel = Column(String(64), index=True)
    draft_thread_ts = Column(String(64), index=True)
    # Текст уведомления сценария — пригодится для AI-промпта
    notification_text = Column(Text)
    # ts активного черновика (Block Kit с кнопками) в drafts-канале — нужно,
    # чтобы при @mention/regenerate обновлять in-place одно сообщение,
    # а не плодить новые.
    active_draft_msg_ts = Column(String(64))
    # Зеркало того же Block Kit-черновика в исходном треде основного канала,
    # чтобы оператор не уходил из этого треда. Обновляется синхронно с drafts.
    mirror_channel = Column(String(64))
    mirror_msg_ts = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)


class ChatappToken(Base):
    """
    Кэш токенов ChatApp (одна строка, id=1). Persistent чтобы переживать рестарты
    контейнера: лимит 100 логинов в сутки на email-appId — переиспользуем токены.
    """
    __tablename__ = "chatapp_tokens"

    id = Column(Integer, primary_key=True)                # всегда 1
    cabinet_user_id = Column(BigInteger)
    access_token = Column(String(512), nullable=False)
    access_token_end_time = Column(BigInteger, nullable=False)   # unix seconds
    refresh_token = Column(String(512), nullable=False)
    refresh_token_end_time = Column(BigInteger, nullable=False)  # unix seconds
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def create_tables() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
