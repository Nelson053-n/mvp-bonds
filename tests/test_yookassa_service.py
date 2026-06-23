"""Unit tests for app.services.yookassa_service (one-off Pro payments).

All outbound YooKassa HTTP is mocked — no real network. Billing keys are blanked
in conftest (settings_override), so each test that needs YooKassa "enabled" sets
shop_id/secret_key on the module's own `settings` reference via monkeypatch.
"""

import base64

import pytest

import app.services.yookassa_service as yk


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Records the last request and returns a canned response (or raises)."""
    last_method = None
    last_url = None
    last_json = None
    last_headers = None

    def __init__(self, *a, response=None, raise_exc=None, **k):
        self._response = response if response is not None else _FakeResp()
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_method = "POST"
        type(self).last_url = url
        type(self).last_json = json
        type(self).last_headers = headers
        if self._raise:
            raise self._raise
        return self._response

    async def get(self, url, headers=None):
        type(self).last_method = "GET"
        type(self).last_url = url
        type(self).last_headers = headers
        if self._raise:
            raise self._raise
        return self._response


def _patch_client(monkeypatch, *, response=None, raise_exc=None):
    def factory(*a, **k):
        return _FakeClient(response=response, raise_exc=raise_exc)
    monkeypatch.setattr(yk.httpx, "AsyncClient", factory)


def _enable(monkeypatch, shop_id="shop-123", secret="live_secret"):
    monkeypatch.setattr(yk.settings, "yookassa_shop_id", shop_id)
    monkeypatch.setattr(yk.settings, "yookassa_secret_key", secret)


# ── enabled() ────────────────────────────────────────────────────────────────

def test_enabled_false_without_keys():
    # conftest blanks the keys; without enabling, billing is off.
    assert yk.enabled() is False


def test_enabled_true_with_keys(monkeypatch):
    _enable(monkeypatch)
    assert yk.enabled() is True


def test_auth_header_is_basic(monkeypatch):
    _enable(monkeypatch, shop_id="abc", secret="def")
    header = yk._auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "abc:def"


# ── create_payment ──────────────────────────────────────────────────────────

async def test_create_payment_happy(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(
        200,
        json_data={
            "id": "pay_1",
            "status": "pending",
            "confirmation": {"confirmation_url": "https://yoomoney.ru/checkout/pay_1"},
        },
    ))
    out = await yk.create_payment(user_id=7, plan="month", idempotence_key="key-1")
    assert out == {
        "id": "pay_1",
        "status": "pending",
        "confirmation_url": "https://yoomoney.ru/checkout/pay_1",
    }
    # request carries the right amount/metadata + idempotence key
    body = _FakeClient.last_json
    assert body["amount"]["value"] == f"{yk.settings.pro_price_month}.00"
    assert body["metadata"] == {"user_id": "7", "plan": "month"}
    assert _FakeClient.last_headers["Idempotence-Key"] == "key-1"


async def test_create_payment_year_plan_uses_year_price(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(
        201, json_data={"id": "p", "status": "pending", "confirmation": {}}))
    await yk.create_payment(1, "year", "k")
    assert _FakeClient.last_json["amount"]["value"] == f"{yk.settings.pro_price_year}.00"


async def test_create_payment_disabled_returns_none(monkeypatch):
    # keys blanked → no network call, returns None
    called = {"n": 0}
    _patch_client(monkeypatch, response=_FakeResp(200))

    out = await yk.create_payment(1, "month", "k")
    assert out is None


async def test_create_payment_invalid_plan(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(200, json_data={"id": "x"}))
    assert await yk.create_payment(1, "lifetime", "k") is None


async def test_create_payment_http_error(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(400, json_data={}, text="bad request"))
    assert await yk.create_payment(1, "month", "k") is None


async def test_create_payment_network_error(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, raise_exc=RuntimeError("dns fail"))
    assert await yk.create_payment(1, "month", "k") is None


# ── get_payment ──────────────────────────────────────────────────────────────

async def test_get_payment_happy(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(
        200, json_data={"id": "pay_1", "status": "succeeded"}))
    out = await yk.get_payment("pay_1")
    assert out == {"id": "pay_1", "status": "succeeded"}
    assert _FakeClient.last_url.endswith("/pay_1")
    assert _FakeClient.last_headers["Authorization"].startswith("Basic ")


async def test_get_payment_disabled_returns_none():
    assert await yk.get_payment("pay_1") is None


async def test_get_payment_empty_id_returns_none(monkeypatch):
    _enable(monkeypatch)
    assert await yk.get_payment("") is None


async def test_get_payment_non_200_returns_none(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, response=_FakeResp(404, json_data={}))
    assert await yk.get_payment("pay_1") is None


async def test_get_payment_network_error(monkeypatch):
    _enable(monkeypatch)
    _patch_client(monkeypatch, raise_exc=RuntimeError("timeout"))
    assert await yk.get_payment("pay_1") is None


# ── _grant_pro_from_payment flow (app/api/billing.py) ────────────────────────
# The webhook re-fetches the payment and, if it really succeeded, grants Pro.

def test_grant_pro_succeeded(settings_override):
    from datetime import date, timedelta
    from app.api.billing import _grant_pro_from_payment
    from app.services.storage_service import storage_service

    uid = storage_service.create_user("yk_buyer", "$2b$dummyhash")
    _amount, days = yk.PLANS["month"]

    granted = _grant_pro_from_payment({
        "id": "pay_ok",
        "status": "succeeded",
        "metadata": {"user_id": str(uid), "plan": "month"},
    })
    assert granted is True

    user = storage_service.get_user_by_id(uid)
    assert user["is_pro"] is True
    # new users already carry a 30-day trial; the renewal stacks from the later
    # of now / current expiry, so pro_until is at least today + month-days.
    assert user["pro_until"] >= (date.today() + timedelta(days=days)).isoformat()


def test_grant_pro_records_payment(settings_override):
    from app.api.billing import _grant_pro_from_payment
    from app.services.storage_service import storage_service

    uid = storage_service.create_user("yk_buyer2", "$2b$dummyhash")
    _grant_pro_from_payment({
        "id": "pay_recorded",
        "status": "succeeded",
        "metadata": {"user_id": str(uid), "plan": "year"},
    })
    ids = [p["payment_id"] for p in storage_service.get_pro_payments()]
    assert "pay_recorded" in ids


def test_grant_pro_not_succeeded():
    from app.api.billing import _grant_pro_from_payment
    assert _grant_pro_from_payment(
        {"id": "p", "status": "pending", "metadata": {"user_id": "1", "plan": "month"}}
    ) is False


def test_grant_pro_bad_user_id():
    from app.api.billing import _grant_pro_from_payment
    assert _grant_pro_from_payment(
        {"id": "p", "status": "succeeded", "metadata": {"plan": "month"}}
    ) is False


def test_grant_pro_unknown_plan():
    from app.api.billing import _grant_pro_from_payment
    assert _grant_pro_from_payment(
        {"id": "p", "status": "succeeded", "metadata": {"user_id": "1", "plan": "weekly"}}
    ) is False


def test_grant_pro_empty_payment():
    from app.api.billing import _grant_pro_from_payment
    assert _grant_pro_from_payment({}) is False
    assert _grant_pro_from_payment(None) is False
