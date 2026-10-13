"""Сессия в HttpOnly-cookie вместо localStorage (аудит 12.10.2026, M8).

Токен в localStorage уносила бы любая XSS. Теперь браузер держит его в
HttpOnly-cookie, JS его не видит. Bearer-заголовок оставлен для скриптов.
CSRF: SameSite=Lax + отказ межсайтовых изменяющих запросов по Sec-Fetch-Site
и Origin, если авторизация пришла из cookie.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import SESSION_COOKIE
from app.main import app
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


async def _fresh(base: str = "http://test"):
    return AsyncClient(transport=ASGITransport(app=app), base_url=base)


async def _register(c, name: str):
    r = await c.post("/auth/register", json={"username": name, "password": "secret12"})
    assert r.status_code == 201, r.text
    return r


async def test_login_sets_httponly_lax_cookie_and_cookie_authenticates() -> None:
    async with await _fresh() as c:
        r = await _register(c, "ck_basic")
        sc = r.headers["set-cookie"].lower()
        assert f"{SESSION_COOKIE}=" in sc and "httponly" in sc and "samesite=lax" in sc
        assert "secure" not in sc                      # http — без Secure, иначе браузер не сохранит
        assert (await c.get("/auth/me")).status_code == 200   # только cookie, без заголовка


async def test_cookie_is_secure_behind_https() -> None:
    async with await _fresh("https://test") as c:
        r = await _register(c, "ck_https")
        assert "secure" in r.headers["set-cookie"].lower()


async def test_cross_site_write_with_cookie_rejected() -> None:
    async with await _fresh() as c:
        await _register(c, "ck_csrf")
        bad = await c.post("/portfolios", json={"name": "X"}, headers={"Origin": "https://evil.example"})
        assert bad.status_code == 403
        bad2 = await c.post("/portfolios", json={"name": "X"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert bad2.status_code == 403
        ok = await c.post("/portfolios", json={"name": "X"}, headers={"Origin": "http://test"})
        assert ok.status_code != 403


async def test_bearer_not_subject_to_origin_check() -> None:
    async with await _fresh() as c:
        tok = (await _register(c, "ck_bearer")).json()["access_token"]
    async with await _fresh() as c2:   # без cookie
        r = await c2.post("/portfolios", json={"name": "Y"},
                          headers={"Authorization": f"Bearer {tok}", "Origin": "https://other.example"})
        assert r.status_code != 403


async def test_legacy_token_exchanged_for_cookie() -> None:
    async with await _fresh() as c:
        tok = (await _register(c, "ck_legacy")).json()["access_token"]
    async with await _fresh() as c2:
        r = await c2.post("/auth/session", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 204 and SESSION_COOKIE in r.headers.get("set-cookie", "")
        assert (await c2.get("/auth/me")).status_code == 200


async def test_logout_clears_cookie() -> None:
    async with await _fresh() as c:
        await _register(c, "ck_logout")
        r = await c.post("/auth/logout")
        assert r.status_code == 204
        assert (await c.get("/auth/me")).status_code == 403


async def test_revoked_cookie_rejected() -> None:
    async with await _fresh() as c:
        await _register(c, "ck_revoked")
        old_cookie = c.cookies.get(SESSION_COOKIE)
        await c.post("/auth/change-password", json={"old_password": "secret12", "new_password": "secret13"},
                     headers={"Origin": "http://test"})
        assert (await c.get("/auth/me")).status_code == 200          # новая cookie от смены пароля
    async with await _fresh() as c2:
        c2.cookies.set(SESSION_COOKIE, old_cookie)
        assert (await c2.get("/auth/me")).status_code == 401
