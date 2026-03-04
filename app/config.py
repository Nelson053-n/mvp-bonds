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

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MVP_")


settings = Settings()
