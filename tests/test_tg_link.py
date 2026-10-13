"""Привязка Telegram только по одноразовой ссылке на бота (аудит 12.10.2026, M2).

Раньше chat_id вводили руками: к своему аккаунту можно было привязать чужой
чат (любого, кто нажимал Start у бота) и слать туда от имени bondai сообщения
с HTML из username. Теперь чат привязывает сам бот — тот, где владелец нажал
Start по ссылке с одноразовым кодом.
"""
import time

import pytest

from app.services.auth_service import auth_service
from app.services.storage_service import storage_service
from app.services.telegram_bot_service import TelegramBotService


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM tg_link_nonces")
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    storage_service.update_user_tg_chat_id(1, None)
    yield
    storage_service.update_user_tg_chat_id(1, None)


def _bot():
    sent: list = []
    bot = TelegramBotService()

    async def fake_send(client, chat_id, text, keyboard=None):
        sent.append((chat_id, text))
    bot._send = fake_send
    return bot, sent


def _start(text: str, chat_id: int = 555, chat_type: str = "private") -> dict:
    return {"message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


async def _link(client, auth_headers) -> str:
    r = await client.post("/auth/me/telegram/link", headers=auth_headers)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("https://t.me/bondinfoai_bot?start=link_")
    return url.split("start=link_", 1)[1]


async def test_link_binds_chat_where_start_pressed(client, auth_headers) -> None:
    nonce = await _link(client, auth_headers)
    bot, sent = _bot()
    await bot._handle_update(None, _start(f"/start link_{nonce}", chat_id=777))
    assert storage_service.get_user_by_id(1)["tg_chat_id"] == "777"
    assert "подключён" in sent[-1][1]


async def test_nonce_single_use(client, auth_headers) -> None:
    nonce = await _link(client, auth_headers)
    bot, sent = _bot()
    await bot._handle_update(None, _start(f"/start link_{nonce}", chat_id=777))
    storage_service.update_user_tg_chat_id(1, None)
    await bot._handle_update(None, _start(f"/start link_{nonce}", chat_id=888))
    assert storage_service.get_user_by_id(1)["tg_chat_id"] is None
    assert "устарела" in sent[-1][1]


async def test_expired_nonce_rejected() -> None:
    storage_service.create_tg_link_nonce(1, "old_nonce", int(time.time()) - 1)
    bot, sent = _bot()
    await bot._handle_update(None, _start("/start link_old_nonce"))
    assert storage_service.get_user_by_id(1)["tg_chat_id"] is None


async def test_group_chat_refused(client, auth_headers) -> None:
    nonce = await _link(client, auth_headers)
    bot, sent = _bot()
    await bot._handle_update(None, _start(f"/start link_{nonce}", chat_type="group"))
    assert storage_service.get_user_by_id(1)["tg_chat_id"] is None


async def test_new_link_invalidates_previous(client, auth_headers) -> None:
    first = await _link(client, auth_headers)
    await _link(client, auth_headers)
    bot, _ = _bot()
    await bot._handle_update(None, _start(f"/start link_{first}"))
    assert storage_service.get_user_by_id(1)["tg_chat_id"] is None


async def test_link_rate_limited(client, auth_headers) -> None:
    codes = [(await client.post("/auth/me/telegram/link", headers=auth_headers)).status_code
             for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


async def test_unlink_allowed(client, auth_headers) -> None:
    storage_service.update_user_tg_chat_id(1, "777")
    r = await client.post("/auth/me/telegram", json={"tg_chat_id": ""}, headers=auth_headers)
    assert r.status_code == 204
    assert storage_service.get_user_by_id(1)["tg_chat_id"] is None


async def test_bot_reply_escapes_username(client, auth_headers, monkeypatch) -> None:
    nonce = await _link(client, auth_headers)
    real = storage_service.get_user_by_id

    def fake_get(uid):
        u = dict(real(uid))
        u["username"] = '<a href="https://evil">x</a>'
        return u
    monkeypatch.setattr(storage_service, "get_user_by_id", fake_get)
    bot, sent = _bot()
    await bot._handle_update(None, _start(f"/start link_{nonce}"))
    assert "<a href" not in sent[-1][1] and "&lt;a href" in sent[-1][1]


async def test_reset_code_message_escapes_username(monkeypatch) -> None:
    from app.services.notification_service import notification_service
    sent: list = []

    async def fake_send(token, chat_id, text):
        sent.append(text)
        return True
    monkeypatch.setattr(notification_service, "send_telegram", fake_send)
    await auth_service._send_telegram_reset("T", "1", "123456", "<b>x</b>")
    assert "<b>x</b>" not in sent[0] and "&lt;b&gt;x&lt;/b&gt;" in sent[0]


@pytest.mark.parametrize("name,ok", [
    ("ivan_99", True), ("Иван.Петров", True), ("user@mail.ru", True), ("a+b-c", True),
    ("<b>x</b>", False), ("evil.com/x", False), ("http://x", False), ("имя с пробелом", False),
])
async def test_username_whitelist(client, name, ok) -> None:
    r = await client.post("/auth/register", json={"username": name, "password": "secret12"})
    assert (r.status_code == 201) is ok, (name, r.status_code, r.text[:120])
