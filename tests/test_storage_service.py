"""
Tests for storage service.
"""

import pytest

from app.services.storage_service import StorageService

# Bootstrap always creates admin user + portfolio with id=1
TEST_PORTFOLIO_ID = 1


class TestStorageService:
    """Tests for StorageService CRUD operations."""

    @pytest.fixture
    def service(self, settings_override) -> StorageService:
        return StorageService()

    def test_add_item(self, service: StorageService) -> None:
        """Test adding an item to storage."""
        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        assert item_id > 0

    def test_get_items_empty(self, service: StorageService) -> None:
        """Test getting items from empty storage."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        items = service.get_items(TEST_PORTFOLIO_ID)

        assert items == []

    def test_get_items_after_add(self, service: StorageService) -> None:
        """Test getting items after adding."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        items = service.get_items(TEST_PORTFOLIO_ID)

        assert len(items) == 1
        assert items[0]["id"] == item_id
        assert items[0]["ticker"] == "TEST"
        assert items[0]["instrument_type"] == "stock"
        assert items[0]["quantity"] == 100.0
        assert items[0]["purchase_price"] == 250.0
        assert items[0]["manual_coupon"] is None

    def test_delete_item(self, service: StorageService) -> None:
        """Test deleting an item."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        deleted = service.delete_item(item_id, TEST_PORTFOLIO_ID)

        assert deleted == 1
        assert service.get_items(TEST_PORTFOLIO_ID) == []

    def test_delete_nonexistent_item(self, service: StorageService) -> None:
        """Test deleting a nonexistent item."""
        deleted = service.delete_item(9999, TEST_PORTFOLIO_ID)

        assert deleted == 0

    def test_update_item(self, service: StorageService) -> None:
        """Test updating an item."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        updated = service.update_item(
            item_id=item_id,
            portfolio_id=TEST_PORTFOLIO_ID,
            quantity=150,
            purchase_price=260.0,
        )

        assert updated == 1
        items = service.get_items(TEST_PORTFOLIO_ID)
        assert items[0]["quantity"] == 150.0
        assert items[0]["purchase_price"] == 260.0

    def test_update_nonexistent_item(self, service: StorageService) -> None:
        """Test updating a nonexistent item."""
        updated = service.update_item(
            item_id=9999,
            portfolio_id=TEST_PORTFOLIO_ID,
            quantity=150,
            purchase_price=260.0,
        )

        assert updated == 0

    def test_update_coupon(self, service: StorageService) -> None:
        """Test updating coupon for a bond."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        item_id = service.add_item(
            ticker="TESTBOND",
            instrument_type="bond",
            quantity=10,
            purchase_price=920.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        updated = service.update_coupon(item_id=item_id, portfolio_id=TEST_PORTFOLIO_ID, coupon=15.5)

        assert updated == 1
        items = service.get_items(TEST_PORTFOLIO_ID)
        assert items[0]["manual_coupon"] == 15.5

    def test_update_coupon_nonexistent(self, service: StorageService) -> None:
        """Test updating coupon for nonexistent item."""
        updated = service.update_coupon(item_id=9999, portfolio_id=TEST_PORTFOLIO_ID, coupon=15.5)

        assert updated == 0

    def test_delete_multiple_items(self, service: StorageService) -> None:
        """Test deleting multiple items."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        id1 = service.add_item("SBER", "stock", 100, 250.0, TEST_PORTFOLIO_ID)
        id2 = service.add_item("GAZP", "stock", 50, 150.0, TEST_PORTFOLIO_ID)
        id3 = service.add_item("TATN", "stock", 30, 300.0, TEST_PORTFOLIO_ID)

        deleted = service.delete_items([id1, id3], TEST_PORTFOLIO_ID)

        assert deleted == 2
        items = service.get_items(TEST_PORTFOLIO_ID)
        assert len(items) == 1
        assert items[0]["id"] == id2

    def test_delete_empty_list(self, service: StorageService) -> None:
        """Test deleting empty list of items."""
        deleted = service.delete_items([], TEST_PORTFOLIO_ID)

        assert deleted == 0

    def test_add_bond_item(self, service: StorageService) -> None:
        """Test adding a bond item."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        item_id = service.add_item(
            ticker="TESTBOND",
            instrument_type="bond",
            quantity=10,
            purchase_price=920.0,
            portfolio_id=TEST_PORTFOLIO_ID,
        )

        assert item_id > 0
        items = service.get_items(TEST_PORTFOLIO_ID)
        assert items[0]["instrument_type"] == "bond"

    def test_items_ordered_by_id(self, service: StorageService) -> None:
        """Test that items are returned ordered by ID."""
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items WHERE portfolio_id = ?", (TEST_PORTFOLIO_ID,))
            conn.commit()

        id1 = service.add_item("SBER", "stock", 100, 250.0, TEST_PORTFOLIO_ID)
        id2 = service.add_item("GAZP", "stock", 50, 150.0, TEST_PORTFOLIO_ID)
        id3 = service.add_item("TATN", "stock", 30, 300.0, TEST_PORTFOLIO_ID)

        items = service.get_items(TEST_PORTFOLIO_ID)

        assert [item["id"] for item in items] == [id1, id2, id3]


class TestSnapshotGuard:
    """Daily-snapshot guard against MOEX-outage zeros (false dip on charts)."""

    @pytest.fixture
    def service(self, settings_override) -> StorageService:
        return StorageService()

    def _fresh_portfolio(self, service: StorageService) -> int:
        # Isolated portfolio with no pre-existing snapshots, so the guard's
        # effect on today's row is unambiguous.
        return service.create_portfolio(user_id=1, name="snap-guard-test")

    def test_skips_zero_value_with_cost(self, service: StorageService) -> None:
        # Simulates MOEX outage: prices never loaded → value 0 while cost > 0.
        from datetime import date
        today = date.today().isoformat()
        pid = self._fresh_portfolio(service)
        service.save_portfolio_snapshot(pid, total_value=0.0, total_cost=100000.0)
        snaps = service.get_portfolio_snapshots(pid)
        # No snapshot must be written for today (the outage zero was skipped).
        assert not any(s["date"] == today for s in snaps)

    def test_writes_real_snapshot(self, service: StorageService) -> None:
        from datetime import date
        today = date.today().isoformat()
        pid = self._fresh_portfolio(service)
        service.save_portfolio_snapshot(pid, total_value=105000.0, total_cost=100000.0)
        snaps = service.get_portfolio_snapshots(pid)
        assert any(s["date"] == today and s["total_value"] == 105000.0 for s in snaps)

    def test_allows_zero_cost_empty_portfolio(self, service: StorageService) -> None:
        # Empty portfolio (cost 0) is a legitimate zero, not an outage — must write.
        from datetime import date
        today = date.today().isoformat()
        pid = self._fresh_portfolio(service)
        service.save_portfolio_snapshot(pid, total_value=0.0, total_cost=0.0)
        snaps = service.get_portfolio_snapshots(pid)
        assert any(s["date"] == today for s in snaps)
