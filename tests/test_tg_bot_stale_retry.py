"""getUpdates повторяется сразу при обрыве keep-alive соединения.

На проде 14–98 раз/сутки «getUpdates error»: ReadError('') через 0.1с —
соединение от прошлого long-poll уже закрыто. Один повтор идёт по новому
соединению; sendMessage не повторяется — иначе сообщение может уйти дважды.
"""
import logging

import httpx

from app.services.telegram_bot_service import TelegramBotService


def _client(fail_times: int, exc_cls=httpx.ReadError) -> tuple[httpx.AsyncClient, list]:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= fail_times:
            raise exc_cls("")
        return httpx.Response(200, json={"ok": True, "result": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def _svc() -> TelegramBotService:
    svc = TelegramBotService()
    svc._token = "T"
    return svc


async def test_get_updates_retries_once_silently(caplog) -> None:
    client, calls = _client(fail_times=1)
    with caplog.at_level(logging.WARNING):
        data = await _svc()._call(client, "getUpdates", offset=0)
    assert data["ok"] is True
    assert len(calls) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_remote_protocol_error_also_retried() -> None:
    client, calls = _client(fail_times=1, exc_cls=httpx.RemoteProtocolError)
    assert (await _svc()._call(client, "getUpdates"))["ok"] is True
    assert len(calls) == 2


async def test_persistent_failure_logged_with_type(caplog) -> None:
    client, calls = _client(fail_times=5)
    with caplog.at_level(logging.WARNING):
        data = await _svc()._call(client, "getUpdates")
    assert data == {"ok": False}
    assert len(calls) == 2
    assert any("ReadError" in r.getMessage() for r in caplog.records)


async def test_send_message_not_retried() -> None:
    client, calls = _client(fail_times=1)
    data = await _svc()._call(client, "sendMessage", chat_id=1, text="x")
    assert data == {"ok": False}
    assert len(calls) == 1
