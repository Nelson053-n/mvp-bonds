"""Tests for coupon-schedule business logic.

Two distinct pieces of logic carry "coupon scheduling":

1. ``CouponScheduleService`` (app/services/coupon_schedule_service.py) — sums the
   coupons actually paid out (per one bond) in the half-open window
   ``(since, until]`` from the MOEX bondization feed, with an in-memory TTL cache.

2. The custom (off-exchange) bond branch in ``PortfolioService.get_table_fresh``
   (app/services/portfolio_service.py) — derives the per-period coupon amount
   (``nominal * rate/100 / freq``), the period in days (``365 // freq``), the
   anchor ``next_coupon_date`` (nearest future payment stepping from the purchase
   date), and the realized-coupon count up to today / maturity.

Both are pure-ish and network-free here: the MOEX fetch is monkeypatched, and the
custom branch never touches MOEX at all.
"""

from datetime import date, timedelta

import pytest

from app.services.coupon_schedule_service import (
    CouponScheduleService,
    _ScheduleCache,
)


class TestRealizedCouponsPerBond:
    """CouponScheduleService.realized_coupons_per_bond — half-open (since, until]."""

    @pytest.fixture
    def service(self) -> CouponScheduleService:
        return CouponScheduleService()

    def _stub_schedule(self, service, schedule):
        """Make _get_schedule return a fixed list, no network."""
        async def fake_get(secid):
            return schedule
        service._get_schedule = fake_get  # type: ignore[assignment]

    async def test_sums_coupons_inside_window(self, service):
        self._stub_schedule(service, [
            (date(2026, 1, 15), 30.0),
            (date(2026, 4, 15), 30.0),
            (date(2026, 7, 15), 30.0),
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=date(2026, 1, 1), until=date(2026, 5, 1)
        )
        # Jan and Apr are inside (Jan1, May1]; Jul is after.
        assert total == 60.0

    async def test_lower_bound_is_exclusive(self, service):
        """A coupon paid exactly on the purchase date went to the previous holder."""
        self._stub_schedule(service, [
            (date(2026, 1, 15), 30.0),  # == since → excluded
            (date(2026, 4, 15), 30.0),
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=date(2026, 1, 15), until=date(2026, 12, 31)
        )
        assert total == 30.0

    async def test_upper_bound_is_inclusive(self, service):
        """A coupon paid exactly on `until` (today) counts."""
        self._stub_schedule(service, [
            (date(2026, 4, 15), 30.0),  # == until → included
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=date(2026, 1, 1), until=date(2026, 4, 15)
        )
        assert total == 30.0

    async def test_no_coupons_in_window_returns_zero(self, service):
        self._stub_schedule(service, [
            (date(2025, 1, 15), 30.0),
            (date(2027, 1, 15), 30.0),
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=date(2026, 1, 1), until=date(2026, 12, 31)
        )
        assert total == 0.0

    async def test_empty_secid_returns_zero(self, service):
        assert await service.realized_coupons_per_bond("", since=date(2026, 1, 1)) == 0.0

    async def test_none_since_returns_zero(self, service):
        assert await service.realized_coupons_per_bond("RU000TEST", since=None) == 0.0

    async def test_until_defaults_to_today(self, service):
        today = date.today()
        self._stub_schedule(service, [
            (today - timedelta(days=1), 12.34),   # paid yesterday → counted
            (today + timedelta(days=1), 99.0),     # future → not counted
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=today - timedelta(days=30)
        )
        assert total == 12.34

    async def test_result_is_rounded(self, service):
        self._stub_schedule(service, [
            (date(2026, 2, 1), 1.111111),
            (date(2026, 3, 1), 2.222222),
        ])
        total = await service.realized_coupons_per_bond(
            "RU000TEST", since=date(2026, 1, 1), until=date(2026, 12, 31)
        )
        assert total == round(1.111111 + 2.222222, 4)


class TestFetchSchedule:
    """_fetch_schedule parses the MOEX bondization `coupons` block."""

    @pytest.fixture
    def service(self) -> CouponScheduleService:
        return CouponScheduleService()

    def _patch_http(self, monkeypatch, *, status=200, payload=None, raise_exc=None):
        class _Resp:
            status_code = status
            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url):
                if raise_exc:
                    raise raise_exc
                return _Resp()

        monkeypatch.setattr(
            "app.services.coupon_schedule_service.httpx.AsyncClient", _Client
        )

    async def test_parses_valid_rows(self, monkeypatch, service):
        self._patch_http(monkeypatch, payload={
            "coupons": {
                "columns": ["coupondate", "value"],
                "data": [
                    ["2026-01-15", 30.5],
                    ["2026-04-15", "30.5"],   # numeric string
                ],
            }
        })
        out = await service._fetch_schedule("RU000TEST")
        assert out == [(date(2026, 1, 15), 30.5), (date(2026, 4, 15), 30.5)]

    async def test_skips_unparseable_date_and_nulls_value(self, monkeypatch, service):
        self._patch_http(monkeypatch, payload={
            "coupons": {
                "columns": ["coupondate", "value"],
                "data": [
                    ["not-a-date", 30.0],   # bad date → skipped row
                    ["2026-04-15", None],    # null value → 0.0
                ],
            }
        })
        out = await service._fetch_schedule("RU000TEST")
        assert out == [(date(2026, 4, 15), 0.0)]

    async def test_missing_columns_returns_empty(self, monkeypatch, service):
        self._patch_http(monkeypatch, payload={"coupons": {"columns": [], "data": []}})
        assert await service._fetch_schedule("RU000TEST") == []

    async def test_unparseable_value_falls_back_to_zero(self, monkeypatch, service):
        """A non-numeric coupon value is coerced to 0.0, not dropped."""
        self._patch_http(monkeypatch, payload={
            "coupons": {
                "columns": ["coupondate", "value"],
                "data": [
                    ["2026-04-15", "n/a"],   # unparseable string → 0.0
                ],
            }
        })
        out = await service._fetch_schedule("RU000TEST")
        assert out == [(date(2026, 4, 15), 0.0)]

    async def test_missing_coupons_block_returns_empty(self, monkeypatch, service):
        """A payload with no `coupons` key → no columns → empty schedule."""
        self._patch_http(monkeypatch, payload={"foo": {}})
        assert await service._fetch_schedule("RU000TEST") == []

    async def test_non_200_returns_none(self, monkeypatch, service):
        self._patch_http(monkeypatch, status=503, payload={})
        assert await service._fetch_schedule("RU000TEST") is None

    async def test_network_error_returns_none(self, monkeypatch, service):
        self._patch_http(monkeypatch, raise_exc=RuntimeError("boom"))
        assert await service._fetch_schedule("RU000TEST") is None


class TestScheduleCache:
    """_ScheduleCache: long TTL for hits, short TTL for misses (None/[])."""

    def test_hit_within_ttl(self, monkeypatch):
        cache = _ScheduleCache()
        t = [1000.0]
        monkeypatch.setattr(
            "app.services.coupon_schedule_service.time.time", lambda: t[0]
        )
        cache.set("X", [(date(2026, 1, 1), 1.0)])
        t[0] += _ScheduleCache.OK_TTL - 1
        value, hit = cache.get("X")
        assert hit is True
        assert value == [(date(2026, 1, 1), 1.0)]

    def test_ok_value_expires_after_ok_ttl(self, monkeypatch):
        cache = _ScheduleCache()
        t = [1000.0]
        monkeypatch.setattr(
            "app.services.coupon_schedule_service.time.time", lambda: t[0]
        )
        cache.set("X", [(date(2026, 1, 1), 1.0)])
        t[0] += _ScheduleCache.OK_TTL + 1
        _, hit = cache.get("X")
        assert hit is False

    def test_miss_uses_short_ttl(self, monkeypatch):
        """None/[] is cached only MISS_TTL so a transient hiccup retries soon."""
        cache = _ScheduleCache()
        t = [1000.0]
        monkeypatch.setattr(
            "app.services.coupon_schedule_service.time.time", lambda: t[0]
        )
        cache.set("X", None)
        # Still fresh within the short TTL.
        t[0] += _ScheduleCache.MISS_TTL - 1
        _, hit = cache.get("X")
        assert hit is True
        # Expires well before OK_TTL would have.
        t[0] += 2
        _, hit = cache.get("X")
        assert hit is False

    def test_unknown_key(self):
        assert _ScheduleCache().get("nope") == (None, False)


class TestGetSchedule:
    """_get_schedule: fetch-through cache wrapper around _fetch_schedule."""

    @pytest.fixture
    def service(self) -> CouponScheduleService:
        return CouponScheduleService()

    async def test_fetches_once_then_serves_from_cache(self, service):
        calls = {"n": 0}
        schedule = [(date(2026, 1, 15), 30.0)]

        async def fake_fetch(secid):
            calls["n"] += 1
            return schedule

        service._fetch_schedule = fake_fetch  # type: ignore[assignment]
        assert await service._get_schedule("RU000TEST") == schedule
        assert await service._get_schedule("RU000TEST") == schedule
        assert calls["n"] == 1  # second call hit the cache

    async def test_none_fetch_returns_empty_list(self, service):
        """A failed fetch (None) surfaces as [] to the caller and is cached."""
        async def fake_fetch(secid):
            return None

        service._fetch_schedule = fake_fetch  # type: ignore[assignment]
        assert await service._get_schedule("RU000TEST") == []
        # None was cached (short MISS_TTL); a fresh hit still returns [].
        cached, hit = service._cache.get("RU000TEST")
        assert hit is True and cached is None


# ---------------------------------------------------------------------------
# Custom (off-exchange) bond coupon scheduling — PortfolioService.get_table_fresh
# ---------------------------------------------------------------------------

class TestCustomBondCouponSchedule:
    """The custom-bond branch derives coupon amount / period / next_coupon_date
    without any MOEX call. We feed one custom item through get_table_fresh by
    monkeypatching the storage layer."""

    @pytest.fixture
    def service(self):
        from app.services.portfolio_service import PortfolioService
        return PortfolioService()

    async def _run(self, monkeypatch, service, item_dict):
        """Build a table for a single custom item and return its only row."""
        from app.services import portfolio_service as ps_mod

        monkeypatch.setattr(
            ps_mod.storage_service, "get_items", lambda pid: [item_dict]
        )
        monkeypatch.setattr(
            ps_mod.storage_service, "get_tbank_coupons", lambda pid: {}
        )

        rows = await service.get_table_fresh(1)
        assert len(rows) == 1
        return rows[0]

    def _base_item(self, **over):
        item = {
            "id": 1,
            "ticker": "OFFEX1",
            "instrument_type": "bond",
            "quantity": 10,
            "purchase_price": 1000.0,
            "manual_coupon": None,
            "manual_coupon_rate": 12.0,   # 12% annual
            "source": "custom",
            "custom_name": "Off-exchange bond",
            "custom_nominal": 1000.0,
            "custom_coupon_freq": 2,      # semiannual
            "purchase_date": None,
            "custom_price": None,
        }
        item.update(over)
        return item

    @pytest.mark.parametrize("freq,expected_period", [(2, 182), (4, 91), (12, 30)])
    async def test_period_for_frequencies(self, monkeypatch, service, freq, expected_period):
        """coupon_period = 365 // freq for 2 / 4 / 12 payments a year."""
        row = await self._run(monkeypatch, service, self._base_item(custom_coupon_freq=freq))
        assert row.coupon_period == expected_period

    @pytest.mark.parametrize("freq,expected_amount", [
        (2, round(1000.0 * 0.12 / 2, 4)),   # 60.0
        (4, round(1000.0 * 0.12 / 4, 4)),   # 30.0
        (12, round(1000.0 * 0.12 / 12, 4)),  # 10.0
    ])
    async def test_coupon_amount_per_period(self, monkeypatch, service, freq, expected_amount):
        """coupon = nominal * rate/100 / freq."""
        row = await self._run(monkeypatch, service, self._base_item(custom_coupon_freq=freq))
        assert row.coupon == expected_amount

    async def test_default_freq_is_semiannual(self, monkeypatch, service):
        """Missing/zero freq falls back to 2 → period 182."""
        row = await self._run(monkeypatch, service, self._base_item(custom_coupon_freq=None))
        assert row.coupon_period == 182
        assert row.coupon == 60.0

    async def test_default_nominal_1000(self, monkeypatch, service):
        """Missing custom_nominal defaults to 1000."""
        row = await self._run(monkeypatch, service, self._base_item(custom_nominal=None))
        assert row.coupon == 60.0

    async def test_next_coupon_date_is_in_future(self, monkeypatch, service):
        """Anchor steps forward from purchase date until it lands today-or-later."""
        buy = (date.today() - timedelta(days=400)).isoformat()
        row = await self._run(monkeypatch, service, self._base_item(purchase_date=buy))
        assert row.next_coupon_date is not None
        assert row.next_coupon_date >= date.today()
        # It must be reachable from the purchase date by whole periods.
        delta = (row.next_coupon_date - date.fromisoformat(buy)).days
        assert delta % row.coupon_period == 0

    async def test_no_rate_no_coupon_fields(self, monkeypatch, service):
        """Without a positive coupon rate there is no derived coupon schedule."""
        row = await self._run(monkeypatch, service, self._base_item(manual_coupon_rate=None))
        assert row.coupon_period is None
        assert row.next_coupon_date is None

    async def test_realized_coupons_counted_since_purchase(self, monkeypatch, service):
        """Realized = full periods elapsed since purchase × per-period × qty."""
        # Bought 1 year ago, semiannual → exactly 2 coupons paid.
        buy = (date.today() - timedelta(days=365)).isoformat()
        row = await self._run(monkeypatch, service, self._base_item(purchase_date=buy))
        # period = 182; 365 // 182 = 2 coupons paid (at +182, +364 days).
        assert row.realized_coupons == round(60.0 * 10 * 2, 2)
        # full_profit = price revaluation (0 here) + realized coupons.
        assert row.full_profit == round(0.0 + 60.0 * 10 * 2, 2)

    async def test_coupon_today_just_after_purchase_not_yet_realized(self, monkeypatch, service):
        """Bought today → no full period elapsed → zero realized coupons."""
        buy = date.today().isoformat()
        row = await self._run(monkeypatch, service, self._base_item(purchase_date=buy))
        assert row.realized_coupons == 0.0
        assert row.full_profit == 0.0

    async def test_realized_capped_at_maturity(self, monkeypatch, service):
        """Coupons stop accruing past the maturity date."""
        buy = (date.today() - timedelta(days=400)).isoformat()
        mat = (date.today() - timedelta(days=200)).isoformat()
        row = await self._run(monkeypatch, service, self._base_item(
            purchase_date=buy, custom_maturity=mat
        ))
        # Window is (buy, maturity] = 200 days; period 182 → exactly 1 coupon.
        assert row.realized_coupons == round(60.0 * 10 * 1, 2)
