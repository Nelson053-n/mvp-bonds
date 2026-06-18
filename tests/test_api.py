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


class TestPublicPagesAndHeaders:
    """Static public pages: HEAD support, content-type, social cards."""

    async def test_uchebnik_get_is_html(self, client: AsyncClient) -> None:
        resp = await client.get("/uchebnik")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "Учебник по облигациям" in resp.text

    @pytest.mark.parametrize("path", ["/uchebnik", "/privacy", "/terms", "/og-image.png"])
    async def test_head_returns_html_not_json(self, client: AsyncClient, path: str) -> None:
        """HEAD must mirror GET's content-type, not fall through to the JSON 404 handler."""
        resp = await client.head(path)
        assert resp.status_code == 200
        ct = resp.headers["content-type"]
        assert not ct.startswith("application/json"), f"{path} HEAD returned {ct}"

    async def test_uchebnik_has_og_image_and_twitter(self, client: AsyncClient) -> None:
        html = (await client.get("/uchebnik")).text
        assert 'property="og:image" content="https://bondai.ru/og-image.png"' in html
        assert 'name="twitter:card" content="summary_large_image"' in html

    @pytest.mark.parametrize("path", ["/privacy", "/terms"])
    async def test_legal_pages_have_og_image(self, client: AsyncClient, path: str) -> None:
        html = (await client.get(path)).text
        assert 'property="og:image"' in html
        assert 'name="twitter:card"' in html

    async def test_uchebnik_has_metrika(self, client: AsyncClient) -> None:
        html = (await client.get("/uchebnik")).text
        assert "107693104" in html
        assert "mc.yandex.ru/metrika/tag.js" in html

    async def test_og_image_served(self, client: AsyncClient) -> None:
        resp = await client.get("/og-image.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"


class TestCustomInstrument:
    """Off-exchange ('custom') items: added without MOEX, priced manually."""

    async def test_add_custom_bond_no_moex(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.post(
            f"/portfolios/{TEST_PORTFOLIO_ID}/instruments",
            json={
                "ticker": "SPB-TEST", "quantity": 5, "purchase_price": 920.0,
                "is_custom": True, "instrument_type": "bond",
                "custom_name": "Облигация СПБ Тест", "current_price": 960.0, "coupon_rate": 14.0,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        item_id = resp.json()["id"]
        # Verify via the fresh table (POST may return a pre-recompute cache row).
        table = await client.get(f"/portfolios/{TEST_PORTFOLIO_ID}/table", headers=auth_headers)
        row = next(r for r in table.json()["items"] if r["id"] == item_id)
        assert row["source"] == "custom"
        assert row["name"] == "Облигация СПБ Тест"
        assert row["current_price"] == 960.0
        assert row["is_traded"] is False
        assert row["profit"] == 200.0  # (960-920)*5

    async def test_edit_custom_price(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.post(
            f"/portfolios/{TEST_PORTFOLIO_ID}/instruments",
            json={"ticker": "SPB-EDIT", "quantity": 2, "purchase_price": 1000.0,
                  "is_custom": True, "instrument_type": "bond", "custom_name": "Edit Me"},
            headers=auth_headers,
        )
        item_id = resp.json()["id"]
        upd = await client.patch(
            f"/portfolios/{TEST_PORTFOLIO_ID}/instruments/{item_id}",
            json={"quantity": 2, "purchase_price": 1000.0, "current_price": 1100.0},
            headers=auth_headers,
        )
        assert upd.status_code == 200
        assert upd.json()["current_price"] == 1100.0
        assert upd.json()["profit"] == 200.0  # (1100-1000)*2


class TestAllAggregation:
    """/all/table?group=ticker merges same security across portfolios."""

    async def test_merge_same_ticker_across_portfolios(self, client: AsyncClient, auth_headers: dict) -> None:
        # Pro needed to create >2 portfolios (free-tier cap).
        await client.patch("/admin/users/1/pro", json={"is_pro": True}, headers=auth_headers)
        # two portfolios (brokers) with the SAME custom bond
        r1 = await client.post("/portfolios", json={"name": "Брокер 1"}, headers=auth_headers)
        r2 = await client.post("/portfolios", json={"name": "Брокер 2"}, headers=auth_headers)
        assert r1.status_code in (200, 201), r1.text
        assert r2.status_code in (200, 201), r2.text
        p1, p2 = r1.json(), r2.json()
        for pid, qty, price in [(p1["id"], 10, 1000.0), (p2["id"], 20, 950.0)]:
            await client.post(
                f"/portfolios/{pid}/instruments",
                json={"ticker": "MERGE1", "quantity": qty, "purchase_price": price,
                      "is_custom": True, "instrument_type": "bond", "custom_name": "Merge Test",
                      "current_price": 1020.0},
                headers=auth_headers,
            )
        # ungrouped: 2 rows
        plain = (await client.get("/portfolios/all/table", headers=auth_headers)).json()["items"]
        assert len([r for r in plain if r["ticker"] == "MERGE1"]) == 2
        # grouped: 1 row, summed qty, weighted-avg price
        grouped = (await client.get("/portfolios/all/table?group=ticker", headers=auth_headers)).json()["items"]
        rows = [r for r in grouped if r["ticker"] == "MERGE1"]
        assert len(rows) == 1
        m = rows[0]
        assert m["quantity"] == 30.0
        assert round(m["purchase_price"], 2) == 966.67  # (10*1000+20*950)/30
        assert m["aggregated"] is True
        assert len(m["brokers"]) == 2
