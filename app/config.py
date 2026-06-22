from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    moex_base_url: str = "https://iss.moex.com/iss"
    sqlite_db_path: str = "data/portfolio.db"
    llm_mode: str = "stub"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    log_level: str = "INFO"
    log_format: str = "json"  # json | text
    jwt_secret: str  # Required, no default — fail fast if not set
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 72
    # SMTP for password reset emails (optional)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    max_portfolios_per_user: int = 50     # Pro safety net (effectively unlimited)
    max_items_per_portfolio: int = 200
    # Freemium limits — applied to non-Pro users only (Pro is unlimited).
    free_max_portfolios: int = 2
    free_max_alerts_per_item: int = 3
    # Public Telegram bond-search bot (optional). When set, a long-polling loop
    # starts in the leader worker and answers /start + free-text ticker queries.
    tg_bot_token: str = ""
    # Search-engine verification & indexing (all optional).
    # yandex_verification: the token from Я.Вебмастер ("Мета-тег"/файл verification)
    #   — served as /yandex_<token>.html and as a <meta> on public pages.
    # google_verification: the content value from GSC "HTML tag" method
    #   — served as a <meta name="google-site-verification"> on public pages.
    # indexnow_key: a self-chosen hex key (8-128 chars). When set, the app serves
    #   /<key>.txt and pings IndexNow (Yandex+Bing) so new/changed URLs index fast.
    yandex_verification: str = ""
    google_verification: str = ""
    indexnow_key: str = ""
    # Donate links (optional). When a link is set, a "Поддержать проект" button
    # appears in the dashboard footer / Telegram bot. Add the real URL later.
    donate_url: str = ""          # ЮMoney / СБП / CloudTips payment URL
    donate_sbp_url: str = ""      # optional separate СБП/QR link
    # YooKassa (ЮKassa) — Pro payments. Both empty → billing disabled (UI hides
    # the "Оформить Pro" buttons). shop_id + secret_key from ЮKassa dashboard
    # → Интеграция → Ключи API. Add to .env later, no redeploy needed.
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    # Webhook IP-allowlist: only official YooKassa subnets may hit /billing/webhook.
    # Set MVP_YOOKASSA_WEBHOOK_IP_CHECK=false to disable (local dev / behind proxy).
    yookassa_webhook_ip_check: bool = True
    # Pro prices in RUB and the days each grants.
    pro_price_month: int = 299
    pro_price_year: int = 2490
    pro_days_month: int = 30
    pro_days_year: int = 365

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MVP_")


settings = Settings()
