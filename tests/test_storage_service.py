"""
Tests for storage service.
"""

import pytest

from app.services.storage_service import StorageService


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
        )

        assert item_id > 0

    def test_get_items_empty(self, service: StorageService) -> None:
        """Test getting items from empty storage."""
        # Clean up any existing items first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        items = service.get_items()

        assert items == []

    def test_get_items_after_add(self, service: StorageService) -> None:
        """Test getting items after adding."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
        )

        items = service.get_items()

        assert len(items) == 1
        assert items[0]["id"] == item_id
        assert items[0]["ticker"] == "TEST"
        assert items[0]["instrument_type"] == "stock"
        assert items[0]["quantity"] == 100.0
        assert items[0]["purchase_price"] == 250.0
        assert items[0]["manual_coupon"] is None

    def test_delete_item(self, service: StorageService) -> None:
        """Test deleting an item."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
        )

        deleted = service.delete_item(item_id)

        assert deleted == 1
        assert service.get_items() == []

    def test_delete_nonexistent_item(self, service: StorageService) -> None:
        """Test deleting a nonexistent item."""
        deleted = service.delete_item(9999)

        assert deleted == 0

    def test_update_item(self, service: StorageService) -> None:
        """Test updating an item."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        item_id = service.add_item(
            ticker="TEST",
            instrument_type="stock",
            quantity=100,
            purchase_price=250.0,
        )

        updated = service.update_item(
            item_id=item_id,
            quantity=150,
            purchase_price=260.0,
        )

        assert updated == 1
        items = service.get_items()
        assert items[0]["quantity"] == 150.0
        assert items[0]["purchase_price"] == 260.0

    def test_update_nonexistent_item(self, service: StorageService) -> None:
        """Test updating a nonexistent item."""
        updated = service.update_item(
            item_id=9999,
            quantity=150,
            purchase_price=260.0,
        )

        assert updated == 0

    def test_update_coupon(self, service: StorageService) -> None:
        """Test updating coupon for a bond."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        item_id = service.add_item(
            ticker="TESTBOND",
            instrument_type="bond",
            quantity=10,
            purchase_price=920.0,
        )

        updated = service.update_coupon(item_id=item_id, coupon=15.5)

        assert updated == 1
        items = service.get_items()
        assert items[0]["manual_coupon"] == 15.5

    def test_update_coupon_nonexistent(self, service: StorageService) -> None:
        """Test updating coupon for nonexistent item."""
        updated = service.update_coupon(item_id=9999, coupon=15.5)

        assert updated == 0

    def test_delete_multiple_items(self, service: StorageService) -> None:
        """Test deleting multiple items."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        id1 = service.add_item("SBER", "stock", 100, 250.0)
        id2 = service.add_item("GAZP", "stock", 50, 150.0)
        id3 = service.add_item("TATN", "stock", 30, 300.0)

        deleted = service.delete_items([id1, id3])

        assert deleted == 2
        items = service.get_items()
        assert len(items) == 1
        assert items[0]["id"] == id2

    def test_delete_empty_list(self, service: StorageService) -> None:
        """Test deleting empty list of items."""
        deleted = service.delete_items([])

        assert deleted == 0

    def test_add_bond_item(self, service: StorageService) -> None:
        """Test adding a bond item."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        item_id = service.add_item(
            ticker="TESTBOND",
            instrument_type="bond",
            quantity=10,
            purchase_price=920.0,
        )

        assert item_id > 0
        items = service.get_items()
        assert items[0]["instrument_type"] == "bond"

    def test_items_ordered_by_id(self, service: StorageService) -> None:
        """Test that items are returned ordered by ID."""
        # Clean up first
        with service._connect() as conn:
            conn.execute("DELETE FROM portfolio_items")
            conn.commit()

        id1 = service.add_item("SBER", "stock", 100, 250.0)
        id2 = service.add_item("GAZP", "stock", 50, 150.0)
        id3 = service.add_item("TATN", "stock", 30, 300.0)

        items = service.get_items()

        assert [item["id"] for item in items] == [id1, id2, id3]
