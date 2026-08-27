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


class TestResetFlag:
    """tg_chat_id_reset отличает «отвязали за него» от «никогда не подключал».

    Без флага баннер пришлось бы показывать всем, у кого пустой chat_id —
    то есть большинству пользователей, ни разу не трогавших Telegram.
    """

    def test_auto_clear_raises_flag(self) -> None:
        """Снятие подписки по 403/400 помечает пользователя."""
        from app.services.storage_service import storage_service

        storage_service.update_user_tg_chat_id(1, "@ls_lx")
        assert storage_service.clear_tg_chat_id("@ls_lx") == 1

        user = storage_service.get_user_by_id(1)
        assert user["tg_chat_id"] is None
        assert user["tg_chat_id_reset"] is True

    def test_entering_valid_id_lowers_flag(self) -> None:
        """Пользователь исправил ввод — баннер больше не нужен."""
        from app.services.storage_service import storage_service

        storage_service.update_user_tg_chat_id(1, "@ls_lx")
        storage_service.clear_tg_chat_id("@ls_lx")
        assert storage_service.get_user_by_id(1)["tg_chat_id_reset"] is True

        storage_service.update_user_tg_chat_id(1, "555111222")
        user = storage_service.get_user_by_id(1)
        assert user["tg_chat_id"] == "555111222"
        assert user["tg_chat_id_reset"] is False

    def test_manual_unlink_keeps_flag_untouched(self) -> None:
        """Обнуление ≠ ввод: флаг гасит только непустое значение.

        Иначе автоснятие (оно тоже пишет NULL) сбрасывало бы собственный флаг.
        """
        from app.services.storage_service import storage_service

        storage_service.update_user_tg_chat_id(1, "@ls_lx")
        storage_service.clear_tg_chat_id("@ls_lx")
        storage_service.update_user_tg_chat_id(1, None)
        assert storage_service.get_user_by_id(1)["tg_chat_id_reset"] is True

    async def test_api_exposes_flag(self, client, auth_headers) -> None:
        from app.services.storage_service import storage_service

        storage_service.update_user_tg_chat_id(1, "@ls_lx")
        storage_service.clear_tg_chat_id("@ls_lx")

        r = await client.get("/auth/me/portfolios-stats", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["tg_chat_id_reset"] is True

    async def test_flag_false_for_untouched_user(self, client, auth_headers) -> None:
        """Тот, кого не трогали, баннер видеть не должен."""
        from app.services.storage_service import storage_service

        storage_service.update_user_tg_chat_id(1, "123456789")
        r = await client.get("/auth/me/portfolios-stats", headers=auth_headers)
        assert r.json()["tg_chat_id_reset"] is False
