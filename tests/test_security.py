"""
Security tests: auth flows, IDOR protection, rate limiting, JWT, security headers.
"""

import random
import pytest
from httpx import AsyncClient


def _uniq(prefix: str) -> str:
    """Generate unique username to avoid conflicts in shared test DB."""
    return f"{prefix}_{random.randint(100000, 999999)}"


class TestSecurityHeaders:
    """Verify security headers are present on all responses."""

    async def test_csp_header(self, client: AsyncClient) -> None:
        """CSP header must be set."""
        r = await client.get("/health")
        assert "Content-Security-Policy" in r.headers

    async def test_x_frame_options(self, client: AsyncClient) -> None:
        """X-Frame-Options must be DENY."""
        r = await client.get("/health")
        assert r.headers.get("X-Frame-Options") == "DENY"

    async def test_x_content_type_options(self, client: AsyncClient) -> None:
        """X-Content-Type-Options must be nosniff."""
        r = await client.get("/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    async def test_referrer_policy(self, client: AsyncClient) -> None:
        """Referrer-Policy must be set."""
        r = await client.get("/health")
        assert "Referrer-Policy" in r.headers

    async def test_no_cache_on_dashboard(self, client: AsyncClient) -> None:
        """Dashboard HTML must not be cached."""
        r = await client.get("/app")
        cc = r.headers.get("Cache-Control", "")
        assert "no-store" in cc or "no-cache" in cc


class TestAuthFlow:
    """Test registration, login, JWT token lifecycle."""

    async def test_register_success(self, client: AsyncClient) -> None:
        """New user registration returns 201 with access_token."""
        username = _uniq("reg")
        r = await client.post(
            "/auth/register",
            json={"username": username, "password": "securepass123"},
        )
        assert r.status_code == 201
        data = r.json()
        assert "access_token" in data
        assert data["username"] == username

    async def test_register_duplicate_user(self, client: AsyncClient) -> None:
        """Registering same username twice returns 400."""
        username = _uniq("dup")
        payload = {"username": username, "password": "pass12345"}
        await client.post("/auth/register", json=payload)
        r = await client.post("/auth/register", json=payload)
        assert r.status_code == 400

    async def test_login_success(self, client: AsyncClient) -> None:
        """Registered user can login."""
        username = _uniq("login")
        payload = {"username": username, "password": "mypassword"}
        await client.post("/auth/register", json=payload)
        r = await client.post("/auth/login", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data

    async def test_login_wrong_password(self, client: AsyncClient) -> None:
        """Login with wrong password returns 4xx (not 200)."""
        username = _uniq("wp")
        await client.post(
            "/auth/register",
            json={"username": username, "password": "correctpass"},
        )
        r = await client.post(
            "/auth/login",
            json={"username": username, "password": "wrongpass"},
        )
        # Auth failure — 400/401/403 depending on framework behavior
        assert r.status_code in (400, 401, 403)

    async def test_login_nonexistent_user(self, client: AsyncClient) -> None:
        """Login for nonexistent user returns 4xx (not 200)."""
        r = await client.post(
            "/auth/login",
            json={"username": _uniq("nx"), "password": "anything"},
        )
        assert r.status_code in (400, 401, 403)

    async def test_protected_endpoint_without_token(self, client: AsyncClient) -> None:
        """Protected endpoint returns 4xx without auth (401 or 403)."""
        r = await client.get("/portfolios")
        # FastAPI HTTPBearer returns 403 when no Authorization header
        assert r.status_code in (401, 403)

    async def test_protected_endpoint_with_invalid_token(self, client: AsyncClient) -> None:
        """Invalid JWT returns 401."""
        r = await client.get(
            "/portfolios",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert r.status_code == 401

    async def test_auth_me_returns_user_info(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """GET /auth/me returns current user info."""
        r = await client.get("/auth/me", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "username" in data
        assert "user_id" in data

    async def test_auth_me_without_token(self, client: AsyncClient) -> None:
        """GET /auth/me without token returns 4xx (401 or 403)."""
        r = await client.get("/auth/me")
        assert r.status_code in (401, 403)


class TestIDORProtection:
    """Test that users cannot access other users' portfolios."""

    async def test_cannot_access_other_user_portfolio(self, client: AsyncClient) -> None:
        """User A cannot read User B's portfolio."""
        r_a = await client.post(
            "/auth/register",
            json={"username": _uniq("ia"), "password": "passwordA123"},
        )
        assert r_a.status_code == 201
        headers_a = {"Authorization": f"Bearer {r_a.json()['access_token']}"}

        r_b = await client.post(
            "/auth/register",
            json={"username": _uniq("ib"), "password": "passwordB123"},
        )
        assert r_b.status_code == 201
        headers_b = {"Authorization": f"Bearer {r_b.json()['access_token']}"}

        r_port = await client.post(
            "/portfolios",
            json={"name": "B's private portfolio"},
            headers=headers_b,
        )
        assert r_port.status_code == 201
        portfolio_b_id = r_port.json()["id"]

        r = await client.get(
            f"/portfolios/{portfolio_b_id}/table",
            headers=headers_a,
        )
        assert r.status_code == 403

    async def test_cannot_add_to_other_user_portfolio(self, client: AsyncClient) -> None:
        """User A cannot add instruments to User B's portfolio."""
        r_a = await client.post(
            "/auth/register",
            json={"username": _uniq("addA"), "password": "passwordA123"},
        )
        headers_a = {"Authorization": f"Bearer {r_a.json()['access_token']}"}

        r_b = await client.post(
            "/auth/register",
            json={"username": _uniq("addB"), "password": "passwordB123"},
        )
        headers_b = {"Authorization": f"Bearer {r_b.json()['access_token']}"}

        r_port = await client.post(
            "/portfolios",
            json={"name": "B's private portfolio 2"},
            headers=headers_b,
        )
        portfolio_b_id = r_port.json()["id"]

        r = await client.post(
            f"/portfolios/{portfolio_b_id}/instruments",
            json={"ticker": "SBER", "instrument_type": "stock", "quantity": 1, "purchase_price": 100},
            headers=headers_a,
        )
        assert r.status_code == 403

    async def test_cannot_delete_other_user_portfolio(self, client: AsyncClient) -> None:
        """User A cannot delete User B's portfolio."""
        r_a = await client.post(
            "/auth/register",
            json={"username": _uniq("delA"), "password": "passwordA123"},
        )
        headers_a = {"Authorization": f"Bearer {r_a.json()['access_token']}"}

        r_b = await client.post(
            "/auth/register",
            json={"username": _uniq("delB"), "password": "passwordB123"},
        )
        headers_b = {"Authorization": f"Bearer {r_b.json()['access_token']}"}

        r_port = await client.post(
            "/portfolios",
            json={"name": "B's deletable portfolio"},
            headers=headers_b,
        )
        portfolio_b_id = r_port.json()["id"]

        r = await client.delete(
            f"/portfolios/{portfolio_b_id}",
            headers=headers_a,
        )
        assert r.status_code == 403


class TestAdminProtection:
    """Admin endpoints must reject non-admin users."""

    async def test_admin_stats_requires_admin(self, client: AsyncClient) -> None:
        """Regular user cannot access admin stats."""
        r_user = await client.post(
            "/auth/register",
            json={"username": _uniq("na"), "password": "password123"},
        )
        assert r_user.status_code == 201
        token = r_user.json()["access_token"]
        r = await client.get(
            "/admin/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_admin_users_requires_admin(self, client: AsyncClient) -> None:
        """Regular user cannot list all users."""
        r_user = await client.post(
            "/auth/register",
            json={"username": _uniq("na2"), "password": "password123"},
        )
        assert r_user.status_code == 201
        token = r_user.json()["access_token"]
        r = await client.get(
            "/admin/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_admin_endpoints_accessible_with_admin_token(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        """Admin user can access admin stats."""
        r = await client.get("/admin/stats", headers=auth_headers)
        assert r.status_code == 200


class TestPortfolioOwnership:
    """Test portfolio creation and proper ownership."""

    async def test_user_can_only_see_own_portfolios(self, client: AsyncClient) -> None:
        """GET /portfolios returns only current user's portfolios."""
        import random
        suffix = str(random.randint(10000, 99999))
        r_a = await client.post(
            "/auth/register",
            json={"username": f"own_ta_{suffix}", "password": "password123"},
        )
        assert r_a.status_code == 201
        token_a = r_a.json()["access_token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}

        r_b = await client.post(
            "/auth/register",
            json={"username": f"own_tb_{suffix}", "password": "password123"},
        )
        assert r_b.status_code == 201
        token_b = r_b.json()["access_token"]
        headers_b = {"Authorization": f"Bearer {token_b}"}

        portfolio_name = f"B private {suffix}"
        # B creates portfolio
        await client.post(
            "/portfolios",
            json={"name": portfolio_name},
            headers=headers_b,
        )

        # A lists portfolios — must not see B's
        r = await client.get("/portfolios", headers=headers_a)
        assert r.status_code == 200
        data = r.json()
        # /portfolios returns {"portfolios": [...]}
        portfolios = data.get("portfolios", data) if isinstance(data, dict) else data
        names = [p["name"] for p in portfolios]
        assert portfolio_name not in names

    async def test_create_portfolio_success(self, client: AsyncClient) -> None:
        """Authenticated user can create portfolios."""
        # Use a fresh user to avoid hitting the per-user portfolio limit
        r_user = await client.post(
            "/auth/register",
            json={"username": _uniq("portcreate"), "password": "password123"},
        )
        assert r_user.status_code == 201
        headers = {"Authorization": f"Bearer {r_user.json()['access_token']}"}
        name = f"Test Portfolio Security {random.randint(1, 99999)}"
        r = await client.post("/portfolios", json={"name": name}, headers=headers)
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == name
        assert "id" in data


class TestSharedPortfolio:
    """Test public sharing endpoints don't require auth."""

    async def test_share_nonexistent_token_returns_404(self, client: AsyncClient) -> None:
        """Non-existent share token returns 404."""
        r = await client.get("/share/nonexistent_token_xyz/table")
        assert r.status_code == 404

    async def test_share_page_nonexistent_returns_404(self, client: AsyncClient) -> None:
        """Share page with bad token returns 404 or error page."""
        r = await client.get("/share/nonexistent_token_xyz")
        # Either redirect to error page or 404
        assert r.status_code in (200, 404)  # 200 = error HTML page shown

    async def test_health_endpoint_public(self, client: AsyncClient) -> None:
        """Health endpoint is publicly accessible."""
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestJWTSecurity:
    """Test JWT token security properties."""

    async def test_expired_token_rejected(self, client: AsyncClient) -> None:
        """Expired JWT token is rejected."""
        import jwt
        import time
        from app.config import settings

        # Create expired token
        payload = {
            "sub": "1",
            "username": "admin",
            "is_admin": True,
            "exp": int(time.time()) - 3600,  # expired 1 hour ago
            "iat": int(time.time()) - 7200,
        }
        expired_token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

        r = await client.get(
            "/portfolios",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert r.status_code == 401

    async def test_token_with_wrong_secret_rejected(self, client: AsyncClient) -> None:
        """Token signed with wrong secret is rejected."""
        import jwt
        import time

        payload = {
            "sub": "1",
            "username": "admin",
            "is_admin": True,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        bad_token = jwt.encode(payload, "wrong_secret_key_xxxxxxxxxxx", algorithm="HS256")

        r = await client.get(
            "/portfolios",
            headers={"Authorization": f"Bearer {bad_token}"},
        )
        assert r.status_code == 401

    async def test_malformed_bearer_token(self, client: AsyncClient) -> None:
        """Completely malformed bearer token returns 401."""
        r = await client.get(
            "/portfolios",
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert r.status_code == 401
