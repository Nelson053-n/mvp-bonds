"""Telegram chat_id — только число: @username Bot API не принимает.

На проде в users.tg_chat_id лежали два «@username» (поле проверялось лишь
по длине). Каждая рассылка на такой адрес возвращала 400 «chat not found» —
17 отказов за неделю, — а сам адрес молча оставался в базе.

Пустая строка остаётся валидной: это отвязка Telegram, а не ошибка ввода.
"""
import pytest
from pydantic import ValidationError

from app.api.auth import UpdateTelegramInput
from app.api.settings import NotificationSettings


VALID = [
    "123456789",
    "-100123456789",     # группа/канал
    "1",
    "-1",
]

INVALID = [
    "@userinfobot",      # ровно то, что попало в прод
    "@ls_lena",
    "username",
    "123abc",
    "12 34",             # пробел внутри
    "+123456789",        # телефон, не chat_id
    "12.34",
    "-",
    "t.me/user",
]


class TestPersonalChatId:
    """POST /auth/me/telegram — персональная привязка."""

    @pytest.mark.parametrize("value", VALID)
    def test_numeric_accepted(self, value: str) -> None:
        assert UpdateTelegramInput(tg_chat_id=value).tg_chat_id == value

    @pytest.mark.parametrize("value", INVALID)
    def test_non_numeric_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            UpdateTelegramInput(tg_chat_id=value)

    def test_empty_allowed_as_unlink(self) -> None:
        """Пустая строка — отвязка: ломать её валидацией нельзя."""
        assert UpdateTelegramInput(tg_chat_id="").tg_chat_id == ""

    def test_surrounding_whitespace_trimmed(self) -> None:
        assert UpdateTelegramInput(tg_chat_id="  12345  ").tg_chat_id == "12345"

    def test_error_message_names_the_fix(self) -> None:
        """Сообщение должно вести к @userinfobot, а не просто ругаться."""
        with pytest.raises(ValidationError) as exc:
            UpdateTelegramInput(tg_chat_id="@someone")
        assert "userinfobot" in str(exc.value)


class TestAdminChatId:
    """POST /settings/notifications — админский канал алертов."""

    @pytest.mark.parametrize("value", VALID)
    def test_numeric_accepted(self, value: str) -> None:
        assert NotificationSettings(tg_chat_id=value).tg_chat_id == value

    @pytest.mark.parametrize("value", INVALID)
    def test_non_numeric_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            NotificationSettings(tg_chat_id=value)

    def test_default_empty_still_valid(self) -> None:
        """Настройки создаются с пустым chat_id до первой настройки."""
        assert NotificationSettings().tg_chat_id == ""


class TestEndpointRejects:
    """Через HTTP: 422, а не молчаливое сохранение."""

    async def test_username_gets_422(self, client, auth_headers) -> None:
        r = await client.post(
            "/auth/me/telegram",
            json={"tg_chat_id": "@userinfobot"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    async def test_numeric_saved(self, client, auth_headers) -> None:
        r = await client.post(
            "/auth/me/telegram",
            json={"tg_chat_id": "987654321"},
            headers=auth_headers,
        )
        assert r.status_code == 204
