"""Verify that _build_analytics_extra produces the same output shape for
both the single-portfolio (/portfolios/{id}/analytics-extra) and the
all-portfolios (/portfolios/all/analytics-extra) endpoints.

These two endpoints were refactored to share a common helper; this test
guards against any future divergence between the two code paths.
"""
import pytest
from unittest.mock import AsyncMock, patch


# ── Unit-level: call _build_analytics_extra directly ─────────────────────────

@pytest.mark.asyncio
async def test_build_analytics_extra_returns_expected_keys():
    """_build_analytics_extra must always return the five canonical keys,
    even when given an empty row list."""
    from app.api.portfolios import _build_analytics_extra

    with patch("app.services.cbr_service.cbr_service.get_key_rate", new=AsyncMock(return_value=16.0)):
        result = await _build_analytics_extra(rows=[], portfolio_ids=[])

    assert set(result.keys()) == {"events", "anomalies", "realized_coupons", "free_cash_rub", "key_rate"}
    assert isinstance(result["events"], list)
    assert isinstance(result["anomalies"], list)
    assert isinstance(result["realized_coupons"], float)
    assert isinstance(result["free_cash_rub"], float)
    assert result["key_rate"] == 16.0


# ── Integration-level: both HTTP endpoints return identical key-set ───────────

async def test_single_and_all_endpoints_return_same_schema(client, auth_headers):
    """The single-portfolio and all-portfolios analytics-extra endpoints must
    return dicts with the exact same top-level keys (parity of _build_analytics_extra)."""
    # Ensure the test user is Pro so both endpoints are reachable.
    await client.patch("/admin/users/1/pro", json={"is_pro": True}, headers=auth_headers)

    # Resolve portfolio id=1 (created by conftest bootstrap).
    r_single = await client.get("/portfolios/1/analytics-extra", headers=auth_headers)
    assert r_single.status_code == 200, r_single.text

    r_all = await client.get("/portfolios/all/analytics-extra", headers=auth_headers)
    assert r_all.status_code == 200, r_all.text

    single_keys = set(r_single.json().keys())
    all_keys = set(r_all.json().keys())
    assert single_keys == all_keys, (
        f"Key-set diverged between single ({single_keys}) and all ({all_keys}) endpoints"
    )
    expected = {"events", "anomalies", "realized_coupons", "free_cash_rub", "key_rate"}
    assert single_keys == expected
