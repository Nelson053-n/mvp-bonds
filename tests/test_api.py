"""
Tests for portfolio API endpoints.
"""

import pytest
from httpx import AsyncClient

# Bootstrap creates admin user (id=1) with portfolio (id=1)
TEST_PORTFOLIO_ID = 1


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
        self, client: AsyncClient, auth_headers: dict, sample_stock_input: dict
    ) -> None:
        """Test validating stock input."""
        response = await client.post(
            f"/portfolios/{TEST_PORTFOLIO_ID}/validate",
            json={"user_input": sample_stock_input},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["instrument_type"] == "stock"
        assert data["validated"] is True

    async def test_validate_bond_input(
        self, client: AsyncClient, auth_headers: dict, sample_bond_input: dict
    ) -> None:
        """Test validating bond input."""
        response = await client.post(
            f"/portfolios/{TEST_PORTFOLIO_ID}/validate",
            json={"user_input": sample_bond_input},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["instrument_type"] == "bond"
        assert data["validated"] is True

    async def test_validate_invalid_input(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Test validating invalid input (pydantic validation)."""
        response = await client.post(
            f"/portfolios/{TEST_PORTFOLIO_ID}/validate",
            json={
                "user_input": {
                    "ticker": "SBER",
                    "quantity": -10,
                    "purchase_price": 250.0,
                }
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    async def test_get_empty_table(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Test getting portfolio table."""
        response = await client.get(
            f"/portfolios/{TEST_PORTFOLIO_ID}/table",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert isinstance(data["items"], list)

    async def test_delete_nonexistent_instrument(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Test deleting nonexistent instrument."""
        response = await client.delete(
            f"/portfolios/{TEST_PORTFOLIO_ID}/instruments/9999",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_cleanup_not_found_empty(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Test cleanup not found with empty portfolio."""
        response = await client.delete(
            f"/portfolios/{TEST_PORTFOLIO_ID}/instruments/cleanup/not-found",
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "deleted_count" in data

    async def test_unauthorized_access(self, client: AsyncClient) -> None:
        """Test that endpoints require authentication."""
        response = await client.get(f"/portfolios/{TEST_PORTFOLIO_ID}/table")

        assert response.status_code == 403  # HTTPBearer returns 403 when no token


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


class TestFreemiumLimits:
    """Tests for freemium plan limits (v3pay)."""

    async def _register_via_api(self, client: AsyncClient, username: str, password: str) -> dict:
        """Register a user via API and return {token, user_id, headers}."""
        import uuid
        uname = f"{username}_{uuid.uuid4().hex[:8]}"
        resp = await client.post("/auth/register", json={"username": uname, "password": password})
        assert resp.status_code == 201, f"Register failed: {resp.json()}"
        data = resp.json()
        return {
            "token": data["access_token"],
            "user_id": data["user_id"],
            "headers": {"Authorization": f"Bearer {data['access_token']}"},
        }

    async def test_free_plan_pdf_blocked(self, client: AsyncClient) -> None:
        """Free plan: PDF export returns 403 FREE_LIMIT_PDF."""
        user = await self._register_via_api(client, "pdfuser", "password12345")

        resp = await client.post("/portfolios", json={"name": "Test"}, headers=user["headers"])
        assert resp.status_code == 201
        pid = resp.json()["id"]

        resp = await client.get(f"/portfolios/{pid}/report.pdf", headers=user["headers"])
        assert resp.status_code == 403
        assert resp.json()["detail"] == "FREE_LIMIT_PDF"

    async def test_free_plan_second_portfolio_blocked(self, client: AsyncClient) -> None:
        """Free plan: creating 2nd portfolio returns 403 FREE_LIMIT_PORTFOLIOS."""
        user = await self._register_via_api(client, "portuser", "password12345")

        # First portfolio (free allows 1)
        resp = await client.post("/portfolios", json={"name": "P1"}, headers=user["headers"])
        assert resp.status_code == 201

        # Second portfolio should be blocked
        resp = await client.post("/portfolios", json={"name": "P2"}, headers=user["headers"])
        assert resp.status_code == 403
        assert resp.json()["detail"] == "FREE_LIMIT_PORTFOLIOS"

    async def test_free_plan_instrument_limit(self, client: AsyncClient, settings_override) -> None:
        """Free plan: adding 11th instrument returns 403 FREE_LIMIT_INSTRUMENTS."""
        from app.services.storage_service import StorageService
        ss = StorageService()

        user = await self._register_via_api(client, "instruser", "password12345")

        resp = await client.post("/portfolios", json={"name": "P"}, headers=user["headers"])
        assert resp.status_code == 201
        pid = resp.json()["id"]

        # Insert 10 items directly into DB (bypass MOEX validation)
        for i in range(10):
            ss.add_item(f"TICKER{i:02d}", "bond", 1.0, 100.0, pid)

        # 11th instrument via API should be blocked immediately (count check before MOEX)
        resp = await client.post(
            f"/portfolios/{pid}/instruments",
            json={"ticker": "TICKER10", "quantity": 1, "purchase_price": 100.0},
            headers=user["headers"],
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "FREE_LIMIT_INSTRUMENTS"

    async def test_admin_set_plan(self, client: AsyncClient, auth_headers: dict) -> None:
        """Admin can set plan for a user."""
        user = await self._register_via_api(client, "planuser", "password12345")

        resp = await client.patch(
            f"/admin/users/{user['user_id']}/plan",
            json={"plan": "pro", "expires_at": None},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_pro_plan_pdf_accessible(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """After admin upgrades to Pro, PDF is accessible (not 403)."""
        user = await self._register_via_api(client, "prouserp", "password12345")

        resp = await client.post("/portfolios", json={"name": "ProTest"}, headers=user["headers"])
        assert resp.status_code == 201
        pid = resp.json()["id"]

        # Upgrade to pro
        resp = await client.patch(
            f"/admin/users/{user['user_id']}/plan",
            json={"plan": "pro", "expires_at": None},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # PDF should not return 403 FREE_LIMIT_PDF
        resp = await client.get(f"/portfolios/{pid}/report.pdf", headers=user["headers"])
        assert resp.status_code != 403 or resp.json().get("detail") != "FREE_LIMIT_PDF"

    async def test_expired_pro_plan_reverts_to_free(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Expired Pro plan → PDF returns 403 FREE_LIMIT_PDF."""
        import time
        user = await self._register_via_api(client, "expireduser", "password12345")

        resp = await client.post("/portfolios", json={"name": "E"}, headers=user["headers"])
        assert resp.status_code == 201
        pid = resp.json()["id"]

        # Set expired pro plan (1 second in the past)
        resp = await client.patch(
            f"/admin/users/{user['user_id']}/plan",
            json={"plan": "pro", "expires_at": int(time.time()) - 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # PDF should return 403 (expired = free)
        resp = await client.get(f"/portfolios/{pid}/report.pdf", headers=user["headers"])
        assert resp.status_code == 403
        assert resp.json()["detail"] == "FREE_LIMIT_PDF"

    async def test_payment_checkout_stub(self, client: AsyncClient, auth_headers: dict) -> None:
        """Payment checkout returns stub response."""
        resp = await client.post("/payments/checkout?plan=pro", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stub"
        assert data["confirmation_url"] is None
        assert "payment_id" in data

    async def test_idor_portfolio_access(self, client: AsyncClient) -> None:
        """User cannot access another user's portfolio (IDOR protection)."""
        victim = await self._register_via_api(client, "victim", "password12345")
        attacker = await self._register_via_api(client, "attacker", "password12345")

        # Victim creates portfolio
        resp = await client.post(
            "/portfolios", json={"name": "VictimPortfolio"}, headers=victim["headers"]
        )
        assert resp.status_code == 201
        victim_pid = resp.json()["id"]

        # Attacker tries to access victim's portfolio
        resp = await client.get(
            f"/portfolios/{victim_pid}/table", headers=attacker["headers"]
        )
        assert resp.status_code == 403
