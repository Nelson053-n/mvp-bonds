"""Токен Telegram-бота читается из БД, а не только из env.

09.09.2026: бот @bondinfoai_bot был жив (getMe вернул ok), токен задан админом
в UI и лежал в app_settings.tg_bot_token — но публичный поиск облигаций в
Telegram молча не работал. telegram_bot_service.enabled смотрел только на
settings.tg_bot_token (env MVP_TG_BOT_TOKEN), а он на проде не задан.

Весь остальной код (notification_service, алерты фоновых задач) берёт токен из
БД через storage_service, поэтому бот был единственным местом с расхождением.
Env оставлен как фолбэк: он старше и им могут пользоваться другие окружения.
"""
import pytest

from app.services import telegram_bot_service as tbs_module
from app.services.telegram_bot_service import telegram_bot_service


@pytest.fixture
def no_env_token(monkeypatch):
    """MVP_TG_BOT_TOKEN не задан — как на проде."""
    monkeypatch.setattr(tbs_module.settings, "tg_bot_token", "", raising=False)


def test_token_taken_from_db_when_env_empty(monkeypatch, no_env_token):
    monkeypatch.setattr(
        tbs_module.storage_service, "get_setting",
        lambda key, default="": "db-token" if key == "tg_bot_token" else default,
    )
    assert telegram_bot_service._resolve_token() == "db-token"
    assert telegram_bot_service.enabled is True


def test_db_token_wins_over_env(monkeypatch):
    """В БД токен задаётся из UI — он свежее того, что лежит в env."""
    monkeypatch.setattr(tbs_module.settings, "tg_bot_token", "env-token", raising=False)
    monkeypatch.setattr(
        tbs_module.storage_service, "get_setting",
        lambda key, default="": "db-token" if key == "tg_bot_token" else default,
    )
    assert telegram_bot_service._resolve_token() == "db-token"


def test_env_used_as_fallback_when_db_empty(monkeypatch):
    monkeypatch.setattr(tbs_module.settings, "tg_bot_token", "env-token", raising=False)
    monkeypatch.setattr(
        tbs_module.storage_service, "get_setting", lambda key, default="": ""
    )
    assert telegram_bot_service._resolve_token() == "env-token"
    assert telegram_bot_service.enabled is True


def test_disabled_when_neither_source_has_token(monkeypatch, no_env_token):
    monkeypatch.setattr(
        tbs_module.storage_service, "get_setting", lambda key, default="": ""
    )
    assert telegram_bot_service._resolve_token() == ""
    assert telegram_bot_service.enabled is False


def test_db_failure_does_not_break_startup(monkeypatch):
    """Недоступная БД на раннем старте не должна ронять запуск — решает env."""
    monkeypatch.setattr(tbs_module.settings, "tg_bot_token", "env-token", raising=False)

    def boom(key, default=""):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tbs_module.storage_service, "get_setting", boom)
    assert telegram_bot_service._resolve_token() == "env-token"


def test_whitespace_only_token_counts_as_absent(monkeypatch, no_env_token):
    """Пробелы в поле UI не должны выглядеть как настроенный бот."""
    monkeypatch.setattr(
        tbs_module.storage_service, "get_setting", lambda key, default="": "   "
    )
    assert telegram_bot_service._resolve_token() == ""
    assert telegram_bot_service.enabled is False
