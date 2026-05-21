"""
Tests for the password-reset flow:
- /auth/forgot-password — username enumeration immune (always "sent"); IP + per-username rate limits.
- /auth/reset-password — single-use atomic consume; per-IP failure rate limit; cross-code attempt burning.
- Storage layer for password_reset_codes table (atomic consume, expiry, burn).

These tests assume the SQLite-backed state introduced in commit 52ec4c2.
"""

import random
import time

import pytest
from httpx import AsyncClient

from app.services.auth_service import auth_service
from app.services.storage_service import storage_service


def _uniq(prefix: str) -> str:
    return f"{prefix}_{random.randint(100000, 999999)}"


@pytest.fixture(autouse=True)
def _reset_state():
    """Wipe reset codes and rate limits before each test so windows don't leak."""
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM password_reset_codes")
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


async def _register(client: AsyncClient, username: str, password: str = "pw_initial_1") -> int:
    r = await client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return int(r.json()["user_id"])


# ───────────────────────────── /forgot-password ─────────────────────────────


class TestForgotPasswordEnumeration:
    """No matter what the user sent, response is constant {'method': 'sent'}."""

    async def test_nonexistent_username_returns_sent(self, client: AsyncClient) -> None:
        r = await client.post(
            "/auth/forgot-password",
            json={"username": _uniq("ghost"), "lang": "ru"},
        )
        assert r.status_code == 200
        assert r.json() == {"method": "sent"}

    async def test_existing_user_no_channels_returns_sent(self, client: AsyncClient) -> None:
        """User exists but has neither tg_chat_id nor email — still 'sent'."""
        username = _uniq("noch")
        await _register(client, username)
        r = await client.post(
            "/auth/forgot-password",
            json={"username": username, "lang": "ru"},
        )
        assert r.json() == {"method": "sent"}

    async def test_no_code_inserted_for_nonexistent_user(self, client: AsyncClient) -> None:
        """We don't want to leak via DB-side timing/storage either: ghost requests must not insert a row."""
        await client.post(
            "/auth/forgot-password",
            json={"username": _uniq("ghost"), "lang": "ru"},
        )
        with storage_service._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM password_reset_codes").fetchone()[0]
        assert count == 0


class TestForgotPasswordRateLimits:
    """Both per-username and per-IP windows mask their effect with the constant 'sent' response."""

    async def test_per_username_window_does_not_leak(self, client: AsyncClient) -> None:
        """Even after the username window is exhausted, response is still 'sent'."""
        username = _uniq("rlu")
        await _register(client, username)
        for _ in range(5):  # window is 3 — 4th+ are masked
            r = await client.post(
                "/auth/forgot-password",
                json={"username": username, "lang": "ru"},
            )
            assert r.status_code == 200
            assert r.json() == {"method": "sent"}

    async def test_per_ip_window_does_not_leak(self, client: AsyncClient) -> None:
        """Hitting many distinct usernames from one IP still always returns 'sent'."""
        for _ in range(25):  # IP cap is 20
            r = await client.post(
                "/auth/forgot-password",
                json={"username": _uniq("ip"), "lang": "ru"},
            )
            assert r.status_code == 200
            assert r.json() == {"method": "sent"}


# ───────────────────────────── storage layer ─────────────────────────────


class TestPasswordResetStorage:
    """Direct unit tests on storage helpers — these are the new primitives."""

    async def test_insert_and_consume_returns_user_id(self, client: AsyncClient) -> None:
        uid = await _register(client, _uniq("st"))
        storage_service.insert_password_reset_code("111111", uid, int(time.time()) + 900)
        consumed = storage_service.consume_password_reset_code("111111", int(time.time()))
        assert consumed == uid

    async def test_consume_is_single_use(self, client: AsyncClient) -> None:
        """Consuming the same code twice returns None on the second call (atomic DELETE…RETURNING)."""
        uid = await _register(client, _uniq("st"))
        storage_service.insert_password_reset_code("222222", uid, int(time.time()) + 900)
        first = storage_service.consume_password_reset_code("222222", int(time.time()))
        second = storage_service.consume_password_reset_code("222222", int(time.time()))
        assert first == uid
        assert second is None

    async def test_expired_code_is_not_consumed(self, client: AsyncClient) -> None:
        uid = await _register(client, _uniq("st"))
        storage_service.insert_password_reset_code("333333", uid, int(time.time()) - 1)
        consumed = storage_service.consume_password_reset_code("333333", int(time.time()))
        assert consumed is None

    async def test_burn_invalidates_after_max_attempts(self, client: AsyncClient) -> None:
        """5 burn calls (== _RESET_CODE_MAX_ATTEMPTS) wipe all live codes."""
        uid = await _register(client, _uniq("st"))
        now = int(time.time())
        storage_service.insert_password_reset_code("aaaaaa", uid, now + 900)
        storage_service.insert_password_reset_code("bbbbbb", uid, now + 900)

        max_attempts = auth_service._RESET_CODE_MAX_ATTEMPTS
        # First (max-1) burns don't delete anything…
        for _ in range(max_attempts - 1):
            assert storage_service.burn_password_reset_attempts(max_attempts, now) == 0
        # …the max-th burn invalidates both.
        invalidated = storage_service.burn_password_reset_attempts(max_attempts, now)
        assert invalidated == 2
        # And neither code is consumable any more.
        assert storage_service.consume_password_reset_code("aaaaaa", now) is None
        assert storage_service.consume_password_reset_code("bbbbbb", now) is None

    async def test_burn_skips_expired_codes(self, client: AsyncClient) -> None:
        """Expired codes are GC'd by burn, not counted in the 'invalidated' tally."""
        uid = await _register(client, _uniq("st"))
        now = int(time.time())
        storage_service.insert_password_reset_code("expir1", uid, now - 1)
        storage_service.insert_password_reset_code("live11", uid, now + 900)
        # Single burn — expired one is GC'd, live one gets attempts=1, nothing invalidated yet.
        assert storage_service.burn_password_reset_attempts(5, now) == 0
        with storage_service._connect() as conn:
            rows = conn.execute("SELECT code, attempts FROM password_reset_codes").fetchall()
        assert rows == [("live11", 1)]


# ───────────────────────────── /reset-password ─────────────────────────────


class TestResetPasswordHappyPath:
    """Full flow: forgot → directly read code from DB → reset → login with new pw."""

    async def test_valid_code_resets_password_and_login_works(self, client: AsyncClient) -> None:
        username = _uniq("h")
        await _register(client, username, password="old_pw_111")

        # Inject a code straight into the table (skips real Telegram/SMTP delivery).
        with storage_service._connect() as conn:
            uid = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()[0]
        storage_service.insert_password_reset_code("ZCODE1", uid, int(time.time()) + 900)

        r = await client.post(
            "/auth/reset-password",
            json={"code": "ZCODE1", "new_password": "new_pw_222"},
        )
        assert r.status_code == 204

        # Old password no longer works.
        r_old = await client.post(
            "/auth/login", json={"username": username, "password": "old_pw_111"}
        )
        assert r_old.status_code == 400

        # New password works.
        r_new = await client.post(
            "/auth/login", json={"username": username, "password": "new_pw_222"}
        )
        assert r_new.status_code == 200

    async def test_code_is_single_use_via_http(self, client: AsyncClient) -> None:
        """Reusing the same code after a successful reset returns 400 (already consumed)."""
        username = _uniq("h2")
        await _register(client, username)
        with storage_service._connect() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        storage_service.insert_password_reset_code("ONCE11", uid, int(time.time()) + 900)

        first = await client.post(
            "/auth/reset-password", json={"code": "ONCE11", "new_password": "fresh_pw_111"}
        )
        assert first.status_code == 204

        second = await client.post(
            "/auth/reset-password", json={"code": "ONCE11", "new_password": "fresh_pw_222"}
        )
        assert second.status_code == 400


class TestResetPasswordFailures:
    """Failure paths: wrong code → 400; brute-force defenses kick in on repeated failures."""

    async def test_invalid_code_returns_400(self, client: AsyncClient) -> None:
        r = await client.post(
            "/auth/reset-password",
            json={"code": "000000", "new_password": "anything9"},
        )
        assert r.status_code == 400

    async def test_per_ip_rate_limit_after_10_failures(self, client: AsyncClient) -> None:
        """11th failed reset attempt from same IP returns 429."""
        # First 10 wrong codes → 400.
        for i in range(10):
            r = await client.post(
                "/auth/reset-password",
                json={"code": "11111" + str(i % 10), "new_password": "x_pw_9999"},
            )
            assert r.status_code == 400, f"iter {i}: got {r.status_code}"
        # 11th → 429.
        r = await client.post(
            "/auth/reset-password",
            json={"code": "999999", "new_password": "x_pw_9999"},
        )
        assert r.status_code == 429

    async def test_successful_reset_does_not_burn_ip_window(self, client: AsyncClient) -> None:
        """A user with a valid code should reset even if the IP window is nearly exhausted.

        Equivalent: after a successful reset, the same IP's window count must NOT have grown.
        """
        # Burn through 9 failures (still under the 10/15m cap).
        for i in range(9):
            r = await client.post(
                "/auth/reset-password",
                json={"code": "fail" + str(i).zfill(2), "new_password": "x_pw_9999"},
            )
            assert r.status_code == 400

        # Now do a successful reset.
        username = _uniq("sw")
        await _register(client, username)
        with storage_service._connect() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        storage_service.insert_password_reset_code("WINOK1", uid, int(time.time()) + 900)
        ok = await client.post(
            "/auth/reset-password", json={"code": "WINOK1", "new_password": "post_ok_pw1"}
        )
        assert ok.status_code == 204

        # We've used 9 IP slots so far. Success added 0. We still have 1 left before 429.
        one_more = await client.post(
            "/auth/reset-password", json={"code": "888888", "new_password": "x_pw_9999"}
        )
        assert one_more.status_code == 400  # the 10th, not yet 429
        blocked = await client.post(
            "/auth/reset-password", json={"code": "888888", "new_password": "x_pw_9999"}
        )
        assert blocked.status_code == 429  # 11th — blocked

    async def test_live_code_dies_after_brute_force_attempts(self, client: AsyncClient) -> None:
        """After _RESET_CODE_MAX_ATTEMPTS wrong submissions, any in-flight code becomes invalid.

        Each failure (under the IP rate limit) burns one attempt against every live code.
        """
        username = _uniq("bf")
        await _register(client, username)
        with storage_service._connect() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        storage_service.insert_password_reset_code("VICTIM", uid, int(time.time()) + 900)

        # _RESET_CODE_MAX_ATTEMPTS wrong submissions from the attacker.
        for i in range(auth_service._RESET_CODE_MAX_ATTEMPTS):
            r = await client.post(
                "/auth/reset-password",
                json={"code": "ATKR" + str(i).zfill(2), "new_password": "atk_pw_99"},
            )
            assert r.status_code == 400

        # Even with the legitimate code, reset now fails — VICTIM has been invalidated.
        r = await client.post(
            "/auth/reset-password",
            json={"code": "VICTIM", "new_password": "legit_pw_9"},
        )
        assert r.status_code == 400
