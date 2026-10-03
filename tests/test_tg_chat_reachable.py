"""Привязка chat_id проверяется тестовым сообщением.

На проде пользователь дважды сохранял chat_id (204), а первая же рассылка
получала 400 «chat not found» и снимала его (AUDIT tg_unsubscribe) — для
пользователя это выглядело как «номер не сохранился». Причина: не нажат
/start у бота. Теперь недоставляемый ID отклоняется в момент сохранения.
"""
import httpx
import pytest

import app.services.notification_service as ns_mod
from app.services.notification_service import notification_service
from app.services.storage_service import storage_service


def _fake_client(monkeypatch, *, status: int = 200, text: str = '{"ok":true}',
                 exc: Exception | None = None) -> list:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if exc is not None:
            raise exc
        return httpx.Response(status, text=text)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        ns_mod.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


class TestCheckChatReachable:
    async def test_ok(self, monkeypatch) -> None:
        calls = _fake_client(monkeypatch)
        assert await notification_service.check_chat_reachable("T", "1") is True
        assert len(calls) == 1

    async def test_chat_not_found(self, monkeypatch) -> None:
        _fake_client(monkeypatch, status=400, text=(
            '{"ok":false,"error_code":400,'
            '"description":"Bad Request: chat not found"}'))
        assert await notification_service.check_chat_reachable("T", "1") is False

    async def test_blocked(self, monkeypatch) -> None:
        _fake_client(monkeypatch, status=403, text=(
            '{"ok":false,"description":"Forbidden: bot was blocked by the user"}'))
        assert await notification_service.check_chat_reachable("T", "1") is False

    async def test_other_400_is_unknown(self, monkeypatch) -> None:
        """Прочие 400 — не про адрес: решать по ним нельзя."""
        _fake_client(monkeypatch, status=400, text='{"description":"Unauthorized"}')
        assert await notification_service.check_chat_reachable("T", "1") is None

    async def test_network_error_is_unknown(self, monkeypatch) -> None:
        _fake_client(monkeypatch, exc=httpx.ConnectError("boom"))
        assert await notification_service.check_chat_reachable("T", "1") is None

    async def test_does_not_unsubscribe(self, monkeypatch) -> None:
        """Проверка не должна стирать чужие привязки этого chat_id."""
        storage_service.update_user_tg_chat_id(1, "555000555")
        _fake_client(monkeypatch, status=400, text='"chat not found"')
        await notification_service.check_chat_reachable("T", "555000555")
        assert storage_service.get_user_by_id(1)["tg_chat_id"] == "555000555"
        storage_service.update_user_tg_chat_id(1, None)


class TestEndpoint:
    @pytest.fixture(autouse=True)
    def _token(self):
        storage_service.set_setting("tg_bot_token", "test-token")
        storage_service.update_user_tg_chat_id(1, None)
        yield
        storage_service.set_setting("tg_bot_token", "")

    def _check(self, monkeypatch, result) -> list:
        seen: list = []

        async def fake(token, chat_id):
            seen.append((token, chat_id))
            return result
        monkeypatch.setattr(notification_service, "check_chat_reachable", fake)
        return seen

    async def _post(self, client, auth_headers, value: str):
        return await client.post(
            "/auth/me/telegram", json={"tg_chat_id": value}, headers=auth_headers,
        )

    async def test_unreachable_rejected_and_not_saved(
            self, client, auth_headers, monkeypatch) -> None:
        seen = self._check(monkeypatch, False)
        r = await self._post(client, auth_headers, "19975908")
        assert r.status_code == 400
        assert "Start" in r.json()["detail"]
        assert seen == [("test-token", "19975908")]
        assert storage_service.get_user_by_id(1)["tg_chat_id"] is None

    async def test_reachable_saved(self, client, auth_headers, monkeypatch) -> None:
        self._check(monkeypatch, True)
        r = await self._post(client, auth_headers, "19975908")
        assert r.status_code == 204
        assert storage_service.get_user_by_id(1)["tg_chat_id"] == "19975908"

    async def test_unknown_still_saved(self, client, auth_headers, monkeypatch) -> None:
        """Сбой сети на нашей стороне не должен мешать сохранению."""
        self._check(monkeypatch, None)
        r = await self._post(client, auth_headers, "19975908")
        assert r.status_code == 204
        assert storage_service.get_user_by_id(1)["tg_chat_id"] == "19975908"

    async def test_unlink_skips_check(self, client, auth_headers, monkeypatch) -> None:
        seen = self._check(monkeypatch, False)
        r = await self._post(client, auth_headers, "")
        assert r.status_code == 204
        assert seen == []
