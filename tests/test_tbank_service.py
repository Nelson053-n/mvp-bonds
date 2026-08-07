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


class TestTLSContext:
    """T-Bank switched to a Russian Trusted Root CA certificate on 2026-08-07.
    That root is in neither certifi nor the system store, so verification failed
    and sync stopped working. Trust is widened for the T-Bank client ONLY."""

    def test_russian_root_ca_bundled(self) -> None:
        """The CA file must ship with the repo — a missing file silently breaks sync."""
        from app.services.tbank_service import _RU_ROOT_CA
        assert _RU_ROOT_CA.is_file(), f"CA bundle missing: {_RU_ROOT_CA}"

    def test_context_trusts_russian_root(self) -> None:
        from app.services.tbank_service import _ssl_context

        subjects = [
            str(field)
            for cert in _ssl_context().get_ca_certs()
            for rdn in cert.get("subject", ())
            for field in rdn
        ]
        assert any("Russian Trusted Root CA" in s for s in subjects)

    def test_verification_stays_enabled(self) -> None:
        """verify=False would make the fix a security hole, not a fix."""
        import ssl

        from app.services.tbank_service import _ssl_context

        ctx = _ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_default_certifi_roots_still_present(self) -> None:
        """The Russian root is ADDED to the usual roots, not swapped in for them."""
        import ssl

        import certifi

        from app.services.tbank_service import _ssl_context

        plain = ssl.create_default_context(cafile=certifi.where())
        assert len(_ssl_context().get_ca_certs()) == len(plain.get_ca_certs()) + 1

    def test_other_services_unaffected(self) -> None:
        """MOEX/YooKassa/Telegram must keep using plain certifi — the widened
        trust is scoped to the T-Bank client only."""
        import ssl

        import certifi

        plain_subjects = [
            str(field)
            for cert in ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
            for rdn in cert.get("subject", ())
            for field in rdn
        ]
        assert not any("Russian Trusted" in s for s in plain_subjects)
