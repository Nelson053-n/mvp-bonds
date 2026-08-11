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


@pytest.mark.asyncio
async def test_realized_coupons_sums_all_sources_not_just_tbank():
    """realized_coupons must sum the per-row figure, whatever its source.

    Each row already carries realized_coupons computed by portfolio_service from
    whichever source applies: the T-Bank operations journal (synced positions),
    the MOEX coupon schedule (manual bonds with a purchase_date) or the
    period-based estimate (custom off-exchange bonds).

    Regression 1: the query read coupon_notifications.amount — a column that does
    not exist — so every call threw OperationalError, was swallowed by the except,
    and the metric silently stayed 0.
    Regression 2: the replacement summed tbank_coupons directly from the DB, which
    counted ONLY synced positions. On prod that covered 130 of 976 bonds — the 96
    manually-added ones with a known purchase date were silently dropped.
    """
    from types import SimpleNamespace
    from app.api.portfolios import _build_analytics_extra

    def _row(ticker: str, realized):
        return SimpleNamespace(
            type="bond", ticker=ticker, name=ticker,
            current_value=100000.0, prev_close_value=100000.0,
            quantity=1000.0, aci=0.0, coupon=None, coupon_period=None,
            next_coupon_date=None, maturity_date=None, offer_date=None,
            buyback_date=None, company_rating=None, ytm=None,
            realized_coupons=realized,
        )

    rows = [
        _row("RU000SYNCED1", 1500.50),   # T-Bank synced
        _row("RU000MANUAL1", 249.50),    # manual bond, from the MOEX schedule
        _row("RU000NODATE1", None),      # no purchase date -> unknown, contributes 0
    ]

    with patch("app.services.cbr_service.cbr_service.get_key_rate", new=AsyncMock(return_value=16.0)):
        result = await _build_analytics_extra(rows=rows, portfolio_ids=[1])

    assert result["realized_coupons"] == 1750.0

    # No rows at all yields 0.0, not an error.
    with patch("app.services.cbr_service.cbr_service.get_key_rate", new=AsyncMock(return_value=16.0)):
        empty = await _build_analytics_extra(rows=[], portfolio_ids=[99999])
    assert empty["realized_coupons"] == 0.0


@pytest.mark.asyncio
async def test_price_drop_anomaly_uses_prev_close():
    """A >=3% fall against the previous close must surface as an anomaly.

    Regression: the check queried "SELECT price FROM price_snapshots WHERE
    ticker = ? ORDER BY recorded_at" — none of those three columns exist
    (the table is item_id/last_price/updated_at and keeps a single latest price
    per item, not a history). It raised OperationalError on every call, so the
    price-drop anomaly never fired once.
    """
    from types import SimpleNamespace
    from app.api.portfolios import _build_analytics_extra

    def _bond(ticker: str, current_value: float, prev_close_value: float | None):
        """Row carrying every field _build_analytics_extra reads."""
        return SimpleNamespace(
            type="bond", ticker=ticker, name=ticker,
            current_value=current_value, prev_close_value=prev_close_value,
            quantity=1000.0, aci=0.0, coupon=None, coupon_period=None,
            next_coupon_date=None, maturity_date=None, offer_date=None,
            buyback_date=None, company_rating=None, ytm=None,
        )

    rows = [
        _bond("RU000TEST001", 96000.0, 100000.0),   # -4.0% -> anomaly
        _bond("RU000TEST002", 99000.0, 100000.0),   # -1.0% -> below threshold
        _bond("RU000TEST003", 50000.0, None),       # no prev close -> must not raise
    ]

    with patch("app.services.cbr_service.cbr_service.get_key_rate", new=AsyncMock(return_value=16.0)):
        result = await _build_analytics_extra(rows=rows, portfolio_ids=[])

    drops = [a for a in result["anomalies"] if a["type"] == "price_drop"]
    assert len(drops) == 1, drops
    assert drops[0]["ticker"] == "RU000TEST001"
    assert "-4.0%" in drops[0]["text"]


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


async def test_missing_price_does_not_invent_a_loss():
    """Бумага без котировки на MOEX не должна давать фиктивный убыток.

    Регрессия: в fallback-ветке стояло profit = -(purchase_price * quantity),
    поэтому позиция, цену которой получить не удалось, выглядела как
    обнулившаяся. На проде так вела себя BIG (акция американской Big Lots,
    приехавшая синком Т-Банка): -923.58 ₽ фиктивного убытка в итоге портфеля.
    Отсутствие котировки — это незнание цены, а не падение до нуля.
    """
    from unittest.mock import AsyncMock, patch
    from app.exceptions import PriceNotFoundError
    from app.services.portfolio_service import portfolio_service

    raw_item = {
        "id": 1, "portfolio_id": 1, "ticker": "BIG", "instrument_type": "stock",
        "quantity": 18.0, "purchase_price": 51.31,
        "manual_coupon": None, "figi": "BBG000J0D904", "source": "tbank",
    }

    with patch("app.services.portfolio_service.storage_service.get_items",
               return_value=[raw_item]), \
         patch("app.services.portfolio_service.storage_service.get_tbank_coupons",
               return_value={}), \
         patch("app.services.moex_service.moex_service.get_stock_snapshot",
               new=AsyncMock(side_effect=PriceNotFoundError("BIG", "акция"))):
        rows = await portfolio_service.get_table_fresh(1)

    assert len(rows) == 1
    row = rows[0]
    assert row.ticker == "BIG"
    assert row.no_market_data is True, "строка должна быть помечена как «нет данных»"
    assert row.profit == 0.0, f"фиктивный убыток вернулся: profit={row.profit}"
    assert row.current_value == 0.0
