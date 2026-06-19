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

    def test_pop_and_clear(self):
        c = self._cache()
        c["a"] = "A"
        assert c.pop("a") == "A"
        assert c.pop("missing", "d") == "d"
        c["b"] = "B"
        c.clear()
        assert len(c) == 0


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
