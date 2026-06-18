"""Tests for T-Bank operations journal aggregation + tbank_coupons storage."""

import pytest

from app.services.storage_service import StorageService
from app.services.tbank_service import TBankService

TEST_PORTFOLIO_ID = 1


class TestOperationsAggregation:
    """_operations_to_coupons_and_buys: coupon sums + earliest buy date per figi."""

    def test_sums_coupons_and_finds_first_buy(self) -> None:
        ops = [
            {"figi": "A", "date": "2025-01-10T00:00:00Z", "operation_type": "OPERATION_TYPE_BUY", "payment": -1000.0},
            {"figi": "A", "date": "2024-12-01T00:00:00Z", "operation_type": "OPERATION_TYPE_BUY", "payment": -500.0},
            {"figi": "A", "date": "2025-02-10T00:00:00Z", "operation_type": "OPERATION_TYPE_COUPON", "payment": 30.5},
            {"figi": "A", "date": "2025-05-10T00:00:00Z", "operation_type": "OPERATION_TYPE_COUPON", "payment": 30.5},
            {"figi": "B", "date": "2025-03-01T00:00:00Z", "operation_type": "OPERATION_TYPE_COUPON", "payment": 12.0},
        ]
        agg = TBankService._operations_to_coupons_and_buys(ops)

        assert agg["A"]["coupons"] == 61.0
        assert agg["A"]["first_buy"] == "2024-12-01T00:00:00Z"
        # B has a coupon but no buy operation in the window
        assert agg["B"]["coupons"] == 12.0
        assert agg["B"]["first_buy"] is None

    def test_ignores_other_operation_types(self) -> None:
        ops = [
            {"figi": "A", "date": "2025-01-10T00:00:00Z", "operation_type": "OPERATION_TYPE_BROKER_FEE", "payment": -5.0},
            {"figi": "A", "date": "2025-01-11T00:00:00Z", "operation_type": "OPERATION_TYPE_SELL", "payment": 900.0},
        ]
        agg = TBankService._operations_to_coupons_and_buys(ops)
        assert agg["A"]["coupons"] == 0.0
        assert agg["A"]["first_buy"] is None

    def test_skips_rows_without_figi(self) -> None:
        ops = [
            {"figi": "", "date": "2025-01-10T00:00:00Z", "operation_type": "OPERATION_TYPE_COUPON", "payment": 10.0},
        ]
        agg = TBankService._operations_to_coupons_and_buys(ops)
        assert agg == {}

    def test_detects_sell(self) -> None:
        ops = [
            {"figi": "A", "date": "2024-01-01T00:00:00Z", "operation_type": "OPERATION_TYPE_BUY", "payment": -1000.0},
            {"figi": "A", "date": "2024-09-01T00:00:00Z", "operation_type": "OPERATION_TYPE_SELL", "payment": 1050.0},
            {"figi": "B", "date": "2024-02-01T00:00:00Z", "operation_type": "OPERATION_TYPE_BUY", "payment": -2000.0},
        ]
        agg = TBankService._operations_to_coupons_and_buys(ops)
        assert agg["A"]["has_sell"] is True
        assert agg["A"]["last_sell"] == "2024-09-01T00:00:00Z"
        assert agg["B"]["has_sell"] is False
        assert agg["B"]["last_sell"] is None


class TestTbankCouponsStorage:
    """Round-trip + ON CONFLICT overwrite for tbank_coupons."""

    @pytest.fixture
    def service(self, settings_override) -> StorageService:
        return StorageService()

    def test_upsert_and_get(self, service: StorageService) -> None:
        service.upsert_tbank_coupons(TEST_PORTFOLIO_ID, "FIGI1", 123.45, "2024-01-01T00:00:00Z")
        result = service.get_tbank_coupons(TEST_PORTFOLIO_ID)
        assert result["FIGI1"]["coupons_total"] == 123.45
        assert result["FIGI1"]["first_buy_date"] == "2024-01-01T00:00:00Z"

    def test_conflict_overwrites_total_keeps_buy_date(self, service: StorageService) -> None:
        service.upsert_tbank_coupons(TEST_PORTFOLIO_ID, "FIGI2", 100.0, "2024-01-01T00:00:00Z")
        # Re-sync with a higher total and no buy date → total updates, buy date preserved
        service.upsert_tbank_coupons(TEST_PORTFOLIO_ID, "FIGI2", 150.0, None)
        result = service.get_tbank_coupons(TEST_PORTFOLIO_ID)
        assert result["FIGI2"]["coupons_total"] == 150.0
        assert result["FIGI2"]["first_buy_date"] == "2024-01-01T00:00:00Z"

    def test_empty_for_unknown_portfolio(self, service: StorageService) -> None:
        assert service.get_tbank_coupons(999999) == {}

    def test_delete_removes_record(self, service: StorageService) -> None:
        service.upsert_tbank_coupons(TEST_PORTFOLIO_ID, "FIGI_DEL", 100.0, "2024-01-01T00:00:00Z")
        assert "FIGI_DEL" in service.get_tbank_coupons(TEST_PORTFOLIO_ID)
        removed = service.delete_tbank_coupons(TEST_PORTFOLIO_ID, "FIGI_DEL")
        assert removed == 1
        assert "FIGI_DEL" not in service.get_tbank_coupons(TEST_PORTFOLIO_ID)
