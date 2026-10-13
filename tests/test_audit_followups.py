"""Доработки после состязательной перепроверки исправлений аудита 12.10.2026."""
import io
import time

import pytest
from starlette.requests import Request

from app.config import settings
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM rate_limits")
        conn.execute("DELETE FROM tg_link_nonces")
        conn.commit()
    yield


async def _user(client, name: str, pro: bool = False) -> tuple[int, dict, str]:
    r = await client.post("/auth/register", json={"username": name, "password": "secret12"})
    assert r.status_code == 201, r.text
    uid = int(r.json()["user_id"])
    if not pro:
        storage_service.set_user_pro(uid, False)
    tok = r.json()["access_token"]
    return uid, {"Authorization": f"Bearer {tok}"}, tok


def _fill_portfolios(uid: int, n: int) -> list[int]:
    have = storage_service.get_portfolios(uid)
    ids = [p["id"] for p in have]
    while len(ids) < n:
        ids.append(storage_service.create_portfolio(uid, f"P{len(ids)}"))
    return ids


# ── M6: остальные пути в обход лимитов ────────────────────────────────────────

async def test_tbank_import_respects_free_portfolio_limit(client, monkeypatch) -> None:
    import app.api.tbank as tb
    uid, h, _ = await _user(client, "fu_tbank")
    _fill_portfolios(uid, settings.free_max_portfolios)

    def boom(*a, **k):
        raise AssertionError("до Т-Банка дойти не должны")
    monkeypatch.setattr(tb, "TBankService", boom)
    r = await client.post("/tbank/import", json={"token": "t.x" * 10, "account_id": "1"}, headers=h)
    assert r.status_code == 400 and "бесплатном" in r.json()["detail"]


async def test_restore_move_merge_respect_item_limit(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_items_per_portfolio", 2)
    uid, h, _ = await _user(client, "fu_items", pro=True)
    a, b = _fill_portfolios(uid, 2)[:2]
    for t in ("AAA1", "AAA2"):
        storage_service.add_item(ticker=t, instrument_type="stock", quantity=1, purchase_price=1, portfolio_id=a)
    gone = storage_service.add_item(ticker="AAA3", instrument_type="stock", quantity=1, purchase_price=1, portfolio_id=b)
    storage_service.delete_item(gone, b)
    storage_service.add_item(ticker="BBB1", instrument_type="stock", quantity=1, purchase_price=1, portfolio_id=b)
    storage_service.add_item(ticker="BBB2", instrument_type="stock", quantity=1, purchase_price=1, portfolio_id=b)
    # restore в полный портфель
    assert (await client.post(f"/portfolios/{b}/instruments/{gone}/restore", headers=h)).status_code == 400
    # move в полный портфель
    item_a = storage_service.get_item_by_ticker(a, "AAA1", "stock")["id"]
    r = await client.post(f"/portfolios/{a}/instruments/{item_a}/move", json={"target_portfolio_id": b}, headers=h)
    assert r.status_code == 400
    # merge, итог которого превысит лимит
    assert (await client.post(f"/portfolios/{a}/merge-into/{b}", headers=h)).status_code == 400


# ── ЮKassa: сбой выдачи Pro не должен «съедать» платёж ─────────────────────────

def test_payment_claim_rolled_back_if_grant_fails(monkeypatch) -> None:
    from app.api import billing
    uid = storage_service.create_user("fu_pay", "x")
    pay = {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "status": "succeeded",
           "metadata": {"user_id": str(uid), "plan": "month"}}
    real = storage_service.set_user_pro

    def fail(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(storage_service, "set_user_pro", fail)
    with pytest.raises(RuntimeError):
        billing._grant_pro_from_payment(pay)
    monkeypatch.setattr(storage_service, "set_user_pro", real)
    before = storage_service.get_user_by_id(uid)["pro_until"]
    assert billing._grant_pro_from_payment(pay) is True       # повтор уведомления проходит
    assert storage_service.get_user_by_id(uid)["pro_until"] != before


# ── auth: смена имени, опциональный вход, 422, статус ссылки ──────────────────

async def test_change_username_rate_limited(client) -> None:
    _, h, _ = await _user(client, "fu_rename")
    codes = [(await client.patch("/auth/me/username", json={"new_username": f"fu_rn_{i}"}, headers=h)).status_code
             for i in range(11)]
    assert codes[:10] == [204] * 10 and codes[10] == 429


async def test_optional_user_rejects_revoked_token(client) -> None:
    from app.api.deps import get_optional_user
    _, h, tok = await _user(client, "fu_opt")
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {tok}".encode())]}
    assert (await get_optional_user(Request(scope))) is not None
    await client.post("/auth/change-password", json={"old_password": "secret12", "new_password": "secret13"}, headers=h)
    assert (await get_optional_user(Request(scope))) is None


async def test_auth_422_detail_is_readable_string(client) -> None:
    r = await client.post("/auth/register", json={"username": "ivan petrov", "password": "secret12"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and "буквы" in r.json()["detail"]


async def test_non_auth_422_keeps_default_format(client, auth_headers) -> None:
    r = await client.post("/portfolios", json={}, headers=auth_headers)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


async def test_link_status_pending_until_bot_consumes(client) -> None:
    from app.services.telegram_bot_service import TelegramBotService
    uid, h, _ = await _user(client, "fu_link")
    storage_service.update_user_tg_chat_id(uid, "111")   # уже был привязан
    url = (await client.post("/auth/me/telegram/link", headers=h)).json()["url"]
    st = (await client.get("/auth/me/telegram/link/status", headers=h)).json()
    assert st["pending"] is True                           # старый chat_id — ещё не успех
    bot = TelegramBotService()

    async def fake_send(*a, **k):
        pass
    bot._send = fake_send
    await bot._handle_update(None, {"message": {"chat": {"id": 111, "type": "private"},
                                                "text": "/start link_" + url.split("link_", 1)[1]}})
    st = (await client.get("/auth/me/telegram/link/status", headers=h)).json()
    assert st == {"pending": False, "tg_chat_id": "111"}


# ── тикер: завершающий перевод строки, расписание купонов ─────────────────────

def test_secid_rejects_trailing_newline() -> None:
    from app.services.moex_service import valid_secid
    assert not valid_secid("SBER\n")


async def test_coupon_schedule_skips_bad_secid(monkeypatch) -> None:
    import app.services.coupon_schedule_service as cs
    made: list = []

    class Spy:  # исключение тут проглотил бы try/except в _fetch_schedule — считаем вызовы
        def __init__(self, *a, **k):
            made.append(1)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise RuntimeError("stop")
    monkeypatch.setattr(cs.httpx, "AsyncClient", Spy)
    assert await cs.CouponScheduleService()._fetch_schedule("A/../../x") is None
    assert made == []
