"""Telegram-уведомление владельцу при переходе синка Т-Банка «ок → ошибка»."""

from unittest.mock import AsyncMock

import pytest

from app.services import tbank_sync_service
from app.services.storage_service import storage_service
from app.services.tbank_service import TBankError

CFG = {"tbank_token_enc": "enc", "tbank_account_id": "acc", "bonds_only": False}


@pytest.fixture
def setup(settings_override, request, monkeypatch):
    uid = storage_service.create_user(f"notify-{request.node.name}", "x")
    storage_service.update_user_tg_chat_id(uid, "12345")
    pid = storage_service.create_portfolio(uid, "Мой <портфель>")
    storage_service.upsert_sync_config(pid, "enc", "pre", "acc", False)
    storage_service.set_setting("tg_bot_token", "bot-token")

    send = AsyncMock(return_value=True)
    monkeypatch.setattr(tbank_sync_service.notification_service, "send_telegram", send)
    monkeypatch.setattr(tbank_sync_service, "decrypt_and_maybe_migrate", lambda enc: ("tok", None))
    yield uid, pid, send
    # БД сессионная: токен бота не должен протечь в другие тесты.
    storage_service.set_setting("tg_bot_token", "")


def _fail_with(monkeypatch, exc: Exception) -> None:
    async def boom(self, **kw):
        raise exc
    monkeypatch.setattr(tbank_sync_service.TBankService, "sync_portfolio", boom)


async def _sync(pid: int) -> None:
    with pytest.raises(Exception):
        await tbank_sync_service.do_sync_one(pid, CFG)


async def test_ok_to_error_sends_once(setup, monkeypatch):
    _, pid, send = setup
    _fail_with(monkeypatch, TBankError("Ошибка Т-Банк API: 504"))
    await _sync(pid)
    assert send.await_count == 1
    token, chat_id, text = send.await_args.args
    assert (token, chat_id) == ("bot-token", "12345")
    assert "Мой &lt;портфель&gt;" in text
    assert "временно недоступен" in text
    assert "504" not in text and "bot-token" not in text


async def test_error_to_error_silent(setup, monkeypatch):
    _, pid, send = setup
    _fail_with(monkeypatch, TBankError("Ошибка Т-Банк API: 504"))
    await _sync(pid)
    await _sync(pid)
    await _sync(pid)
    assert send.await_count == 1


async def test_pending_removal_to_error_sends(setup, monkeypatch):
    _, pid, send = setup
    storage_service.update_sync_status(pid, "2026-01-01T00:00:00+00:00", "PENDING_REMOVAL:SU1")
    _fail_with(monkeypatch, TBankError("Ошибка Т-Банк API: 504"))
    await _sync(pid)
    assert send.await_count == 1


async def test_ok_to_ok_silent(setup, monkeypatch):
    _, pid, send = setup

    async def ok(self, **kw):
        return {"added": 0, "updated": 0, "removed_candidates": [], "errors": []}
    monkeypatch.setattr(tbank_sync_service.TBankService, "sync_portfolio", ok)
    await tbank_sync_service.do_sync_one(pid, CFG)
    await tbank_sync_service.do_sync_one(pid, CFG)
    assert send.await_count == 0


async def test_auth_error_message_and_sync_disabled(setup, monkeypatch):
    _, pid, send = setup
    _fail_with(monkeypatch, TBankError("Неверный токен Т-Банка"))
    await _sync(pid)
    assert send.await_count == 1
    text = send.await_args.args[2]
    assert "токен недействителен" in text and "отключена" in text
    assert storage_service.get_sync_config(pid)["sync_enabled"] is False


async def test_no_tg_chat_id_silent(setup, monkeypatch):
    uid, pid, send = setup
    storage_service.update_user_tg_chat_id(uid, None)
    _fail_with(monkeypatch, TBankError("Ошибка Т-Банк API: 504"))
    await _sync(pid)  # TBankError пробрасывается, другого исключения нет
    assert send.await_count == 0


async def test_send_failure_does_not_break_sync(setup, monkeypatch):
    _, pid, send = setup
    send.side_effect = RuntimeError("telegram down")
    _fail_with(monkeypatch, TBankError("Ошибка Т-Банк API: 504"))
    with pytest.raises(TBankError):  # исходная ошибка, не RuntimeError
        await tbank_sync_service.do_sync_one(pid, CFG)
    assert send.await_count == 1
    assert storage_service.get_sync_config(pid)["last_sync_error"]
