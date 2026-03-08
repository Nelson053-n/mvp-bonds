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
    max_portfolios_per_user: int = 10
    max_items_per_portfolio: int = 200

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MVP_")


settings = Settings()
