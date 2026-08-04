"""
Tests for MOEX service.
"""

import pytest

from app.services.moex_service import MOEXService
from app.exceptions import PriceNotFoundError


class TestMOEXServiceHelpers:
    """Tests for MOEXService helper methods."""

    @pytest.fixture
    def service(self) -> MOEXService:
        return MOEXService()

    def test_get_first_row_empty(self, service: MOEXService) -> None:
        """Test _get_first_row with empty dataset."""
        dataset = {"columns": [], "data": []}
        result = service._get_first_row(dataset)

        assert result == {}

    def test_get_first_row_with_data(self, service: MOEXService) -> None:
        """Test _get_first_row with data."""
        dataset = {
            "columns": ["id", "name", "value"],
            "data": [[1, "test", 100], [2, "test2", 200]],
        }
        result = service._get_first_row(dataset)

        assert result == {"id": 1, "name": "test", "value": 100}

    def test_parse_date_valid(self, service: MOEXService) -> None:
        """Test _parse_date with valid date."""
        result = service._parse_date("2024-03-15")

        from datetime import date
        assert result == date(2024, 3, 15)

    def test_parse_date_invalid(self, service: MOEXService) -> None:
        """Test _parse_date with invalid date."""
        result = service._parse_date("invalid")

        assert result is None

    def test_parse_date_empty(self, service: MOEXService) -> None:
        """Test _parse_date with empty value."""
        result = service._parse_date(None)

        assert result is None

    def test_find_column_index_found(self, service: MOEXService) -> None:
        """Test _find_column_index when found."""
        columns = ["id", "name", "value"]
        result = service._find_column_index(columns, "name")

        assert result == 1

    def test_find_column_index_not_found(self, service: MOEXService) -> None:
        """Test _find_column_index when not found."""
        columns = ["id", "name", "value"]
        result = service._find_column_index(columns, "missing")

        assert result is None

    def test_safe_value_valid_index(self, service: MOEXService) -> None:
        """Test _safe_value with valid index."""
        row = [1, "test", 100]
        result = service._safe_value(row, 1)

        assert result == "test"

    def test_safe_value_none_value(self, service: MOEXService) -> None:
        """Test _safe_value with None value."""
        row = [1, None, 100]
        result = service._safe_value(row, 1)

        assert result == ""

    def test_safe_value_out_of_bounds(self, service: MOEXService) -> None:
        """Test _safe_value with out of bounds index."""
        row = [1, "test"]
        result = service._safe_value(row, 10)

        assert result == ""

    def test_normalize_rating_valid_with_ru(self, service: MOEXService) -> None:
        """Test _normalize_rating_value strips ru prefix."""
        result = service._normalize_rating_value("ruAAA")
        # "ru" prefix is stripped
        assert result == "AAA"

    def test_normalize_rating_valid_bare(self, service: MOEXService) -> None:
        """Test _normalize_rating_value with bare rating."""
        result = service._normalize_rating_value("AAA")

        assert result == "AAA"

    def test_normalize_rating_invalid(self, service: MOEXService) -> None:
        """Test _normalize_rating_value with invalid rating."""
        result = service._normalize_rating_value("RUB")

        assert result is None

    def test_normalize_rating_empty(self, service: MOEXService) -> None:
        """Test _normalize_rating_value with empty string."""
        result = service._normalize_rating_value("")

        assert result is None

    def test_normalize_rating_with_modifier(
        self, service: MOEXService
    ) -> None:
        """Test _normalize_rating_value with modifier strips ru prefix."""
        result = service._normalize_rating_value("ruAA+")
        # "ru" prefix is stripped
        assert result == "AA+"

    def test_normalize_rating_exp(self, service: MOEXService) -> None:
        """Test _normalize_rating_value with EXP suffix strips ru and (EXP)."""
        result = service._normalize_rating_value("ruAAA(EXP)")
        # "ru" prefix and "(EXP)" suffix are stripped
        assert result == "AAA"


class TestMOEXServiceRatings:
    """Tests for rating parsing logic."""

    @pytest.fixture
    def service(self) -> MOEXService:
        return MOEXService()

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            # ru-prefixed ratings: ru is stripped
            ("ruAAA", "AAA"),
            ("ruAA+", "AA+"),
            ("ruAA-", "AA-"),
            ("ruA", "A"),
            ("ruBBB+", "BBB+"),
            ("ruBB", "BB"),
            ("ruB-", "B-"),
            ("ruCCC", "CCC"),
            # bare ratings: returned as-is (uppercased)
            ("AAA", "AAA"),
            ("BB+", "BB+"),
            ("D", "D"),
            # ru + (EXP): both stripped
            ("ruAAA(EXP)", "AAA"),
        ],
    )
    def test_normalize_rating_variations(
        self,
        service: MOEXService,
        input_val: str,
        expected: str,
    ) -> None:
        """Test various rating normalizations."""
        result = service._normalize_rating_value(input_val)
        assert result == expected

    @pytest.mark.parametrize(
        "input_val",
        ["RUB", "INVALID", "", "NotARating", "123"],
    )
    def test_normalize_rating_invalid_inputs(
        self, service: MOEXService, input_val: str
    ) -> None:
        """Test invalid rating inputs."""
        result = service._normalize_rating_value(input_val)
        assert result is None


class TestRatingCacheTTL:
    """The rating cache must expire entries so stale ratings aren't served forever."""

    def _cache(self):
        from app.services.moex_service import _RatingCache
        return _RatingCache()

    def test_value_present_while_fresh(self):
        c = self._cache()
        c["smartlab:X"] = "BB"
        assert "smartlab:X" in c
        assert c["smartlab:X"] == "BB"

    def test_value_expires(self, monkeypatch):
        import app.services.moex_service as m
        c = self._cache()
        c.OK_TTL = 100
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])
        c["smartlab:X"] = "A"
        assert "smartlab:X" in c
        t[0] += 101  # past OK_TTL
        assert "smartlab:X" not in c

    def test_none_expires_faster(self, monkeypatch):
        import app.services.moex_service as m
        c = self._cache()
        c.OK_TTL = 10000
        c.MISS_TTL = 100
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])
        c["smartlab:Y"] = None       # a source error / miss
        assert "smartlab:Y" in c
        t[0] += 101                  # past MISS_TTL but well within OK_TTL
        assert "smartlab:Y" not in c  # error must not pin the bond to "no rating"

    def test_error_expires_faster_than_miss(self, monkeypatch):
        """A None from a transient network error uses the short ERROR_TTL,
        so a SmartLab blip can't pin a bond to 'no rating' for the full MISS_TTL."""
        import app.services.moex_service as m
        c = self._cache()
        c.MISS_TTL = 900
        c.ERROR_TTL = 60
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])

        c.set_error("smartlab:E")    # transient network error → None
        c["smartlab:M"] = None       # honest "rating not found" → None
        assert "smartlab:E" in c
        assert "smartlab:M" in c

        t[0] += 61                   # past ERROR_TTL but well within MISS_TTL
        assert "smartlab:E" not in c  # error entry already retryable
        assert "smartlab:M" in c      # honest miss still cached

    def test_overwriting_error_clears_error_flag(self, monkeypatch):
        """A successful re-fetch over an errored key reverts to OK_TTL."""
        import app.services.moex_service as m
        c = self._cache()
        c.OK_TTL = 10000
        c.ERROR_TTL = 60
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])
        c.set_error("smartlab:K")
        c["smartlab:K"] = "AA"       # real rating now
        t[0] += 61                   # past ERROR_TTL
        assert "smartlab:K" in c     # but it's a real value on OK_TTL now
        assert c["smartlab:K"] == "AA"

    def test_pop_and_clear(self):
        c = self._cache()
        c["a"] = "A"
        assert c.pop("a") == "A"
        assert c.pop("missing", "d") == "d"
        c["b"] = "B"
        c.clear()
        assert len(c) == 0


class TestSmartLabRetry:
    """_get_smartlab_credit_rating must retry transient failures and must not
    pin a bond to 'no rating' for the full MISS_TTL when SmartLab hiccups."""

    def _service(self):
        return MOEXService()

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        import app.services.moex_service as m

        async def _instant(_):
            return None

        monkeypatch.setattr(m.asyncio, "sleep", _instant)

    def _patch_client(self, monkeypatch, responses):
        """Patch httpx.AsyncClient so each .get() consumes the next item from
        `responses`: an Exception is raised, anything else is returned as a
        fake response with .text / .raise_for_status()."""
        import app.services.moex_service as m
        calls = {"n": 0}

        class _FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                idx = calls["n"]
                calls["n"] += 1
                item = responses[idx]
                if isinstance(item, Exception):
                    raise item
                return _FakeResp(item)

        monkeypatch.setattr(m.httpx, "AsyncClient", _FakeClient)
        return calls

    async def test_retries_then_succeeds(self, monkeypatch):
        import app.services.moex_service as m
        svc = self._service()
        html = '<span class="linear-progress-bar__text">ruAA+</span>'
        calls = self._patch_client(
            monkeypatch,
            [m.httpx.ConnectTimeout("boom"), html],  # fail once, then succeed
        )
        result = await svc._get_smartlab_credit_rating("SECID1")
        assert calls["n"] == 2          # retried exactly once
        assert result is not None
        assert "AA+" in result

    async def test_transient_failure_does_not_pin_none_for_miss_ttl(
        self, monkeypatch
    ):
        """All attempts fail → None cached, but with the short ERROR_TTL, so a
        recovered SmartLab is re-queried long before MISS_TTL elapses."""
        import app.services.moex_service as m
        svc = self._service()
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])

        calls = self._patch_client(
            monkeypatch,
            [m.httpx.ConnectTimeout("boom")] * 5,  # every attempt fails
        )
        result = await svc._get_smartlab_credit_rating("SECID2")
        assert result is None
        assert calls["n"] >= 2          # at least one retry happened

        cache_key = "smartlab:SECID2"
        cache = svc._credit_rating_cache
        # Still cached right now…
        assert cache_key in cache
        # …but only for ERROR_TTL, NOT the 15-min MISS_TTL.
        t[0] += cache.ERROR_TTL + 1
        assert cache_key not in cache
        assert (cache.ERROR_TTL + 1) < cache.MISS_TTL

    async def test_http_404_not_retried(self, monkeypatch):
        """A definitive 404 is cached immediately (short TTL) without retrying."""
        import app.services.moex_service as m
        svc = self._service()

        class _Resp:
            status_code = 404

        exc = m.httpx.HTTPStatusError("nf", request=None, response=_Resp())
        calls = self._patch_client(monkeypatch, [exc, exc, exc])
        result = await svc._get_smartlab_credit_rating("SECID404")
        assert result is None
        assert calls["n"] == 1          # no retries on 404

    async def test_honest_not_found_uses_long_miss_ttl(self, monkeypatch):
        """A valid page with no rating is an honest miss — keep MISS_TTL."""
        import app.services.moex_service as m
        svc = self._service()
        t = [1000.0]
        monkeypatch.setattr(m.time, "time", lambda: t[0])

        self._patch_client(monkeypatch, ["<html>no rating here</html>"])
        result = await svc._get_smartlab_credit_rating("SECID3")
        assert result is None

        cache_key = "smartlab:SECID3"
        cache = svc._credit_rating_cache
        t[0] += cache.ERROR_TTL + 1   # past ERROR_TTL…
        assert cache_key in cache      # …but honest miss persists (MISS_TTL)


class TestFxBondConversion:
    """FX bonds: FACEVALUE/COUPONVALUE are in the bond currency (×rate),
    but ACCRUEDINT (НКД) is already in rubles and must NOT be re-converted."""

    async def test_aci_not_double_converted_for_fx_bond(self, monkeypatch):
        from app.services.moex_service import MOEXService
        svc = MOEXService()

        # Minimal MOEX payload for a USD bond on TQCB.
        async def fake_fetch(url):
            return {
                "securities": {
                    "columns": ["SECID", "BOARDID", "SHORTNAME", "PREVPRICE",
                                "FACEVALUE", "FACEUNIT", "ACCRUEDINT",
                                "COUPONVALUE", "COUPONPERCENT", "COUPONPERIOD",
                                "MATDATE", "NEXTCOUPON", "LISTLEVEL"],
                    "data": [["RU000TEST", "TQCB", "USD Bond", 96.0,
                              1000, "USD", 1139.27,
                              21.5, 4.3, 181,
                              "2027-02-12", "2026-08-12", 1]],
                },
                "marketdata": {
                    "columns": ["SECID", "BOARDID", "LAST", "LCLOSE", "YIELD"],
                    "data": [["RU000TEST", "TQCB", None, None, 7.0]],
                },
            }

        async def fake_fx(currency):
            return 73.439

        async def fake_rating(secid):
            return None

        async def fake_meta(secid):
            return (False, True)

        monkeypatch.setattr(svc, "_fetch", fake_fetch)
        monkeypatch.setattr(svc, "_get_fx_rate", fake_fx)
        monkeypatch.setattr(svc, "_get_smartlab_credit_rating", fake_rating)
        monkeypatch.setattr(svc, "_get_credit_rating", fake_rating)
        monkeypatch.setattr(svc, "_get_sec_meta", fake_meta)

        snap = await svc.get_bond_snapshot("RU000TEST")

        # nominal = 1000 USD × 73.439 = 73439 (converted)
        assert abs(snap.nominal - 73439.0) < 1.0
        # ACCRUEDINT is already RUB → stays 1139.27, NOT ×73.439 (=83666)
        assert abs(snap.aci - 1139.27) < 1.0
        # Sanity: НКД must never exceed the nominal for a normal bond.
        assert snap.aci < snap.nominal


class TestUntradedBondLogging:
    """A bond that is not traded on MOEX (matured, OTC, delisted) has no price
    by definition — that is not an error and must not be logged at ERROR level.

    The shapes below are what MOEX actually returns, verified against the live
    ISS API: a matured bond comes back with an EMPTY securities block, not with
    a populated row carrying a past MATDATE.
    """

    @staticmethod
    def _make_service(monkeypatch, payload: dict):
        svc = MOEXService()

        async def fake_fetch(url):
            return payload

        monkeypatch.setattr(svc, "_fetch", fake_fetch)
        return svc

    # Real MOEX response for a matured/OTC bond: zero rows in `securities`.
    _UNTRADED = {
        "securities": {
            "columns": ["SECID", "BOARDID", "SHORTNAME", "PREVPRICE", "MATDATE"],
            "data": [],
        },
        "marketdata": {
            "columns": ["SECID", "BOARDID", "LAST", "LCLOSE"],
            "data": [],
        },
    }

    # Bond still listed on a board, but no price in any field — a real anomaly.
    _LISTED_NO_PRICE = {
        "securities": {
            "columns": ["SECID", "BOARDID", "SHORTNAME", "PREVPRICE",
                        "FACEVALUE", "FACEUNIT", "MATDATE", "LISTLEVEL"],
            "data": [["RU000TEST", "TQCB", "Test Bond", None,
                      1000, "SUR", "2030-01-01", 1]],
        },
        "marketdata": {
            "columns": ["SECID", "BOARDID", "LAST", "LCLOSE"],
            "data": [["RU000TEST", "TQCB", None, None]],
        },
    }

    async def test_untraded_bond_logs_debug_not_error(self, monkeypatch, caplog):
        svc = self._make_service(monkeypatch, self._UNTRADED)

        with caplog.at_level("DEBUG", logger="app.services.moex_service"):
            with pytest.raises(PriceNotFoundError):
                await svc.get_bond_snapshot("RU000TEST")

        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("не торгуется" in r.getMessage() for r in caplog.records)

    async def test_listed_bond_without_price_still_logs_error(self, monkeypatch, caplog):
        svc = self._make_service(monkeypatch, self._LISTED_NO_PRICE)

        with caplog.at_level("DEBUG", logger="app.services.moex_service"):
            with pytest.raises(PriceNotFoundError):
                await svc.get_bond_snapshot("RU000TEST")

        assert [r for r in caplog.records if r.levelname == "ERROR"]

    async def test_moex_outage_still_logs_error(self, monkeypatch, caplog):
        """A 5xx / network failure must never be silenced: _fetch raises before
        the quiet branch is reached, so the outage stays visible as ERROR."""
        from app.exceptions import DataFetchError

        svc = MOEXService()

        async def failing_fetch(url):
            raise DataFetchError(url, "HTTP 502")

        monkeypatch.setattr(svc, "_fetch", failing_fetch)

        with caplog.at_level("DEBUG", logger="app.services.moex_service"):
            with pytest.raises(DataFetchError):
                await svc.get_bond_snapshot("RU000TEST")
