"""Пожертвования через ЮKassa: платёж на выбранную сумму, Pro не выдаётся."""
import pytest

from app.api import billing
from app.services import yookassa_service as yk
from app.services.storage_service import storage_service

PID = "2c1d9a3e-000f-5000-8000-1b2c3d4e5f60"


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM pro_payments")
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


@pytest.fixture
def created(monkeypatch):
    calls: list = []

    async def fake_create(user_id, amount, idempotence_key):
        calls.append((user_id, amount))
        return {"id": PID, "confirmation_url": "https://yookassa.example/pay"}

    monkeypatch.setattr(yk, "enabled", lambda: True)
    monkeypatch.setattr(yk, "create_donation", fake_create)
    return calls


async def test_donate_returns_confirmation_url(client, auth_headers, created) -> None:
    r = await client.post("/billing/donate", headers=auth_headers, json={"amount": 300})
    assert r.status_code == 200
    assert r.json()["confirmation_url"] == "https://yookassa.example/pay"
    assert created == [(1, 300)]


@pytest.mark.parametrize("amount", [0, 49, 50_001, -5])
async def test_donate_rejects_bad_amount(client, auth_headers, created, amount) -> None:
    r = await client.post("/billing/donate", headers=auth_headers, json={"amount": amount})
    assert r.status_code == 422 and not created


async def test_donate_requires_login_and_billing(client, auth_headers, monkeypatch) -> None:
    assert (await client.post("/billing/donate", json={"amount": 100})).status_code in (401, 403)
    monkeypatch.setattr(yk, "enabled", lambda: False)
    assert (await client.post("/billing/donate", headers=auth_headers, json={"amount": 100})).status_code == 503


async def test_donate_rate_limited(client, auth_headers, created) -> None:
    for _ in range(10):
        assert (await client.post("/billing/donate", headers=auth_headers, json={"amount": 100})).status_code == 200
    assert (await client.post("/billing/donate", headers=auth_headers, json={"amount": 100})).status_code == 429


async def test_create_donation_body(monkeypatch) -> None:
    sent: dict = {}

    async def fake__create(body, key):
        sent.update(body)
        return {"id": PID, "confirmation_url": "u"}

    monkeypatch.setattr(yk, "enabled", lambda: True)
    monkeypatch.setattr(yk, "_create", fake__create)
    await yk.create_donation(7, 250, "k")
    assert sent["amount"] == {"value": "250.00", "currency": "RUB"}
    assert sent["metadata"] == {"user_id": "7", "kind": "donation"}
    assert sent["capture"] is True and "donate=thanks" in sent["confirmation"]["return_url"]


def _payment(status="succeeded", value="300.00", kind="donation"):
    return {"id": PID, "status": status, "amount": {"value": value, "currency": "RUB"},
            "metadata": {"user_id": "1", "kind": kind}}


def test_donation_recorded_without_pro(monkeypatch) -> None:
    before = storage_service.get_user_by_id(1)["pro_until"]
    assert billing._record_donation(_payment()) is True
    rows = storage_service.get_pro_payments()
    assert len(rows) == 1 and rows[0]["plan"] == "donation" and rows[0]["amount"] == 300.0
    assert storage_service.get_user_by_id(1)["pro_until"] == before  # Pro не менялся


def test_donation_duplicate_notification_recorded_once() -> None:
    billing._record_donation(_payment())
    billing._record_donation(_payment())
    assert len(storage_service.get_pro_payments()) == 1


@pytest.mark.parametrize("payment", [_payment(status="pending"), _payment(status="canceled"), _payment(value="0.00")])
def test_donation_not_recorded_unless_succeeded(payment) -> None:
    assert billing._record_donation(payment) is False
    assert storage_service.get_pro_payments() == []


async def test_webhook_routes_donation_not_to_pro(client, monkeypatch) -> None:
    granted, recorded = [], []

    async def fake_get(pid):
        return _payment()

    monkeypatch.setattr(billing.settings, "yookassa_webhook_ip_check", False)
    monkeypatch.setattr(yk, "get_payment", fake_get)
    monkeypatch.setattr(billing, "_grant_pro_from_payment", lambda p: granted.append(p))
    monkeypatch.setattr(billing, "_record_donation", lambda p: recorded.append(p))
    r = await client.post("/billing/webhook", json={"object": {"id": PID}})
    assert r.status_code == 200
    assert recorded and not granted
