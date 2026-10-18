"""Техподдержка: пользователь пишет обращение → админу уходит Telegram, в админке список."""
import pytest

from app.api import support as support_api
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM support_tickets")
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


@pytest.fixture
def tg_sent(monkeypatch):
    sent: list = []

    async def fake_send(token, chat_id, text):
        sent.append((token, chat_id, text))
        return True

    monkeypatch.setattr(support_api.notification_service, "send_telegram", fake_send)
    monkeypatch.setattr(
        support_api.storage_service, "get_all_settings",
        lambda: {"tg_bot_token": "T", "tg_chat_id": "42"},
    )
    return sent


async def test_ticket_saved_and_admin_notified(client, auth_headers, tg_sent) -> None:
    r = await client.post("/support", headers=auth_headers, json={"message": "Не грузится <b>портфель</b>"})
    assert r.status_code == 201
    assert len(tg_sent) == 1
    token, chat_id, text = tg_sent[0]
    assert (token, chat_id) == ("T", "42")
    assert f"#{r.json()['id']}" in text
    assert "&lt;b&gt;портфель" in text and "<b>портфель" not in text  # HTML пользователя экранирован
    rows = storage_service.list_support_tickets()
    assert len(rows) == 1 and rows[0]["status"] == "open"


async def test_ticket_kept_when_telegram_fails(client, auth_headers, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("tg down")

    monkeypatch.setattr(support_api.notification_service, "send_telegram", boom)
    monkeypatch.setattr(
        support_api.storage_service, "get_all_settings",
        lambda: {"tg_bot_token": "T", "tg_chat_id": "42"},
    )
    r = await client.post("/support", headers=auth_headers, json={"message": "Привет, нужна помощь"})
    assert r.status_code == 201
    assert storage_service.count_open_support_tickets() == 1


async def test_requires_login_and_validates(client, auth_headers, tg_sent) -> None:
    assert (await client.post("/support", json={"message": "без входа"})).status_code in (401, 403)
    assert (await client.post("/support", headers=auth_headers, json={"message": "  а "})).status_code == 422
    assert (await client.post("/support", headers=auth_headers, json={"message": "я" * 2001})).status_code == 422
    assert storage_service.count_open_support_tickets() == 0 and not tg_sent


async def test_rate_limit(client, auth_headers, tg_sent) -> None:
    for i in range(5):
        assert (await client.post("/support", headers=auth_headers, json={"message": f"обращение {i}"})).status_code == 201
    assert (await client.post("/support", headers=auth_headers, json={"message": "шестое"})).status_code == 429


async def test_admin_list_and_mark_done(client, auth_headers, tg_sent) -> None:
    await client.post("/support", headers=auth_headers, json={"message": "первое обращение"})
    r = await client.get("/admin/support", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["open_count"] == 1
    t = body["tickets"][0]
    assert t["message"] == "первое обращение" and t["username"]

    assert (await client.patch(f"/admin/support/{t['id']}", headers=auth_headers, json={"done": True})).status_code == 200
    assert (await client.get("/admin/support?status_filter=open", headers=auth_headers)).json()["tickets"] == []
    assert (await client.get("/admin/support", headers=auth_headers)).json()["open_count"] == 0
    assert (await client.patch("/admin/support/99999", headers=auth_headers, json={"done": True})).status_code == 404


async def test_admin_endpoints_closed_to_regular_user(client, tg_sent) -> None:
    from app.api.deps import get_current_user  # noqa: F401
    r = await client.get("/admin/support")
    assert r.status_code in (401, 403)
