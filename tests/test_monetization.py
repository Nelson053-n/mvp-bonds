"""Monetization: Pro tier flag, donate links, Pro-gated AI analysis."""

import pytest

from app.services.storage_service import storage_service
from app.services.llm_service import LLMService


# ── Pro grant / revoke (admin) ───────────────────────────────────────────────

async def test_grant_pro_requires_admin(client):
    r = await client.patch("/admin/users/1/pro", json={"is_pro": True})
    assert r.status_code in (401, 403)


async def test_grant_and_revoke_pro(client, auth_headers):
    # admin user is id=1; grant lifetime Pro
    r = await client.patch("/admin/users/1/pro", json={"is_pro": True}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["is_pro"] is True
    assert storage_service.get_user_by_id(1)["is_pro"] is True

    # revoke
    r = await client.patch("/admin/users/1/pro", json={"is_pro": False}, headers=auth_headers)
    assert r.status_code == 200
    assert storage_service.get_user_by_id(1)["is_pro"] is False


async def test_grant_pro_with_future_expiry(client, auth_headers):
    r = await client.patch("/admin/users/1/pro",
                           json={"is_pro": True, "pro_until": "2999-01-01"}, headers=auth_headers)
    assert r.status_code == 200
    assert storage_service.get_user_by_id(1)["is_pro"] is True


async def test_grant_pro_expired_is_inactive(client, auth_headers):
    r = await client.patch("/admin/users/1/pro",
                           json={"is_pro": True, "pro_until": "2000-01-01"}, headers=auth_headers)
    assert r.status_code == 200
    # past expiry → effective Pro is False
    assert storage_service.get_user_by_id(1)["is_pro"] is False


async def test_grant_pro_bad_date(client, auth_headers):
    r = await client.patch("/admin/users/1/pro",
                           json={"is_pro": True, "pro_until": "not-a-date"}, headers=auth_headers)
    assert r.status_code == 400


# ── /auth/me exposes is_pro ──────────────────────────────────────────────────

async def test_me_includes_is_pro(client, auth_headers):
    await client.patch("/admin/users/1/pro", json={"is_pro": True}, headers=auth_headers)
    r = await client.get("/auth/me", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["is_pro"] is True


# ── Donate info ──────────────────────────────────────────────────────────────

async def test_donate_info_disabled_by_default(client):
    r = await client.get("/donate-info")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


async def test_donate_info_enabled_when_url_set(client, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod.settings, "donate_url", "https://yoomoney.ru/to/xxx", raising=False)
    r = await client.get("/donate-info")
    assert r.json()["enabled"] is True
    assert r.json()["url"] == "https://yoomoney.ru/to/xxx"


# ── Pro-gated AI analysis ────────────────────────────────────────────────────

async def test_ai_analysis_blocked_for_non_pro(client, auth_headers):
    await client.patch("/admin/users/1/pro", json={"is_pro": False}, headers=auth_headers)
    # need an owned portfolio; admin bootstrap has portfolio id=1
    r = await client.get("/portfolios/1/ai-analysis", headers=auth_headers)
    assert r.status_code == 403


async def test_ai_analysis_unavailable_without_key(client, auth_headers):
    await client.patch("/admin/users/1/pro", json={"is_pro": True}, headers=auth_headers)
    r = await client.get("/portfolios/1/ai-analysis", headers=auth_headers)
    # Pro passes the gate; with no OpenAI key it returns available:False, not 500
    assert r.status_code == 200
    assert r.json()["available"] is False


# ── LLM analyze_portfolio without a key ──────────────────────────────────────

async def test_analyze_portfolio_no_key_returns_unavailable():
    svc = LLMService()
    svc.mode = "stub"
    out = await svc.analyze_portfolio([{"ticker": "X", "value_rub": 100}])
    assert out["available"] is False
    assert out["points"] == []


def test_real_ai_available_flag():
    svc = LLMService()
    svc.mode = "stub"
    assert svc.real_ai_available is False
