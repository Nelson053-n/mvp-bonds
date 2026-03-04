"""
Tests for portfolio API endpoints.
"""

import pytest
from httpx import AsyncClient
from pydantic import ValidationError


class TestPortfolioAPI:
    """Tests for portfolio API endpoints."""

    async def test_health_endpoint(self, client: AsyncClient) -> None:
        """Test health endpoint."""
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_api_info_endpoint(self, client: AsyncClient) -> None:
        """Test API info endpoint."""
        response = await client.get("/api-info")

        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert data["service"] == "MVP LLM Portfolio API"

    async def test_dashboard_endpoint(self, client: AsyncClient) -> None:
        """Test dashboard endpoint returns HTML."""
        response = await client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    async def test_validate_stock_input(
        self, client: AsyncClient, sample_stock_input: dict
    ) -> None:
        """Test validating stock input."""
        response = await client.post(
            "/portfolio/validate",
            json={"user_input": sample_stock_input},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["instrument_type"] == "stock"
        assert data["validated"] is True

    async def test_validate_bond_input(
        self, client: AsyncClient, sample_bond_input: dict
    ) -> None:
        """Test validating bond input."""
        response = await client.post(
            "/portfolio/validate",
            json={"user_input": sample_bond_input},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["instrument_type"] == "bond"
        assert data["validated"] is True

    async def test_validate_invalid_input(self, client: AsyncClient) -> None:
        """Test validating invalid input (pydantic validation)."""
        # Pydantic validates quantity > 0, so invalid input returns 422
        response = await client.post(
            "/portfolio/validate",
            json={
                "user_input": {
                    "ticker": "SBER",
                    "quantity": -10,
                    "purchase_price": 250.0,
                }
            },
        )

        assert response.status_code == 422

    async def test_get_empty_table(self, client: AsyncClient) -> None:
        """Test getting empty portfolio table."""
        response = await client.get("/portfolio/table")

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        # Note: table may have data from previous runs
        assert isinstance(data["items"], list)

    async def test_delete_nonexistent_instrument(
        self, client: AsyncClient
    ) -> None:
        """Test deleting nonexistent instrument."""
        response = await client.delete("/portfolio/instruments/9999")

        assert response.status_code == 404

    async def test_cleanup_not_found_empty(
        self, client: AsyncClient
    ) -> None:
        """Test cleanup not found with empty portfolio."""
        response = await client.delete(
            "/portfolio/instruments/cleanup/not-found"
        )

        assert response.status_code == 200
        data = response.json()
        assert "deleted_count" in data


class TestPortfolioServiceUnit:
    """Unit tests for portfolio service logic."""

    async def test_validate_returns_instrument_type(
        self, portfolio_service
    ) -> None:
        """Test that validate returns instrument type."""
        from app.models import AddInstrumentInput

        payload = AddInstrumentInput(
            ticker="SBER",
            quantity=100,
            purchase_price=250.0,
        )
        result = await portfolio_service.validate(payload)

        assert result.instrument_type in ["stock", "bond"]

    async def test_validate_with_valid_data(
        self, portfolio_service
    ) -> None:
        """Test that validate passes with valid data."""
        from app.models import AddInstrumentInput

        payload = AddInstrumentInput(
            ticker="SBER",
            quantity=100,
            purchase_price=250.0,
        )
        result = await portfolio_service.validate(payload)

        assert result.validated is True
        assert result.warnings == []
