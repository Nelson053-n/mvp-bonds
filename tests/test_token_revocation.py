"""Отзыв JWT и права из БД (аудит 12.10.2026, M5).

Раньше токен жил свои 72 часа при любой смене пароля, а is_admin брался из
токена: разжалованный админ сохранял /admin/*, а после захвата аккаунта сброс
пароля жертвой атакующего не выкидывал.
"""
import time

import jwt
import pytest

from app.config import settings
from app.services.auth_service import auth_service
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM rate_limits")
        conn.execute("DELETE FROM password_reset_codes")
        conn.commit()
    yield


async def _user(client, name: str, pw: str = "pass_one1") -> tuple[int, str]:
    r = await client.post("/auth/register", json={"username": name, "password": pw})
    assert r.status_code == 201, r.text
    return int(r.json()["user_id"]), r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def test_password_change_revokes_old_tokens_and_returns_new(client) -> None:
    uid, old = await _user(client, "rev_change")
    other_session = (await client.post("/auth/login", json={"username": "rev_change", "password": "pass_one1"})).json()["access_token"]
    r = await client.post("/auth/change-password", json={"old_password": "pass_one1", "new_password": "pass_two2"}, headers=_h(old))
    assert r.status_code == 200
    new = r.json()["access_token"]
    assert (await client.get("/auth/me", headers=_h(old))).status_code == 401
    assert (await client.get("/auth/me", headers=_h(other_session))).status_code == 401
    assert (await client.get("/auth/me", headers=_h(new))).status_code == 200


async def test_relogin_after_password_change_works(client) -> None:
    """Вход после смены пароля выдаёт токен с актуальной версией."""
    uid, tok = await _user(client, "rev_relogin")
    await client.post("/auth/change-password", json={"old_password": "pass_one1", "new_password": "pass_two2"}, headers=_h(tok))
    r = await client.post("/auth/login", json={"username": "rev_relogin", "password": "pass_two2"})
    assert r.status_code == 200
    assert (await client.get("/auth/me", headers=_h(r.json()["access_token"]))).status_code == 200


async def test_password_reset_revokes_tokens(client) -> None:
    uid, tok = await _user(client, "rev_reset")
    storage_service.insert_password_reset_code("246810", uid, int(time.time()) + 900)
    r = await client.post("/auth/reset-password", json={"username": "rev_reset", "code": "246810", "new_password": "pass_new3"})
    assert r.status_code == 204
    assert (await client.get("/auth/me", headers=_h(tok))).status_code == 401


async def test_admin_flag_read_from_db(client) -> None:
    """Токен с is_admin=True, а в БД пользователь не админ → /admin закрыт."""
    uid, _ = await _user(client, "rev_fake_admin")
    forged = auth_service.create_token(uid, "rev_fake_admin", is_admin=True)
    assert (await client.get("/admin/stats", headers=_h(forged))).status_code == 403


async def test_deleted_user_token_rejected(client) -> None:
    uid, tok = await _user(client, "rev_deleted")
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.commit()
    assert (await client.get("/auth/me", headers=_h(tok))).status_code == 401


async def test_legacy_token_without_tv_still_valid(client) -> None:
    """Токены, выданные до миграции (без "tv"), — версия 0: массового разлогина нет."""
    uid, _ = await _user(client, "rev_legacy")
    payload = {"sub": str(uid), "username": "rev_legacy", "is_admin": False,
               "exp": int(time.time()) + 3600, "iat": int(time.time())}
    legacy = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert (await client.get("/auth/me", headers=_h(legacy))).status_code == 200
