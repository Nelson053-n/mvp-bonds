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
