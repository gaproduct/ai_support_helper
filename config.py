"""
Centralised settings loaded from environment variables / .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql://postgres:postgres@localhost:5432/support_tickets"

    # ── Flomni ────────────────────────────────────────────────────────────────
    flomni_api_key: str = ""
    flomni_api_base_url: str = "https://api.flomni.com"
    # Optional shared secret to verify incoming webhooks from Flomni
    flomni_webhook_secret: str = ""

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-nano"

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = ""   # e.g. https://ircgrovmmxlqjjyzvvzb.supabase.co
    supabase_key: str = ""   # service_role or anon key

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_channel_id: str = "C0AUCC0PLG3"  # support-ai-drafts
    # Канал, в который дублируются посты сценариев (compliance/finance/accounting)
    # для дальнейшей работы операторов через ChatOps (@mention → AI draft → button).
    slack_drafts_channel_id: str = ""
    # Для проверки подписи входящих Slack Events / Interactions.
    slack_signing_secret: str = ""

    # ── ChatApp ───────────────────────────────────────────────────────────────
    # Все вызовы идут через chatapp_client.py со строгим whitelist (5 методов).
    chatapp_base_url: str = "https://api.chatapp.online"
    chatapp_email: str = ""
    chatapp_password: str = ""
    chatapp_app_id: str = ""
    chatapp_license_id: str = ""
    # Список через запятую: "WhatsApp,Telegram,grWhatsApp" и т.п.
    chatapp_messenger_types: str = ""
    # ISO-8601, с какого момента качаем историю при backfill.
    chatapp_history_from: str = "2026-05-01T00:00:00Z"

    # ── App ───────────────────────────────────────────────────────────────────
    webhook_port: int = 8000
    log_level: str = "INFO"


settings = Settings()
