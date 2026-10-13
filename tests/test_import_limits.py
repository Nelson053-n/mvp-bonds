"""Лимиты тарифа действуют и при импорте (аудит 12.10.2026, M6).

import-all создавал портфели в обход free_max_portfolios, а CSV-импорт и
пакетное добавление — бумаги в обход max_items_per_portfolio.
"""
import io

import pytest

from app.config import settings
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


async def _free_user(client, name: str) -> tuple[int, dict]:
    r = await client.post("/auth/register", json={"username": name, "password": "secret12"})
    assert r.status_code == 201, r.text
    uid = int(r.json()["user_id"])
    storage_service.set_user_pro(uid, False)  # регистрация даёт триал Pro — снимаем
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _csv(rows: list[str], header: str) -> dict:
    data = (header + "\n" + "\n".join(rows) + "\n").encode()
    return {"file": ("p.csv", io.BytesIO(data), "text/csv")}


async def test_import_all_respects_free_portfolio_limit(client) -> None:
    uid, h = await _free_user(client, "imp_free")
    rows = [f"P{i},SBER,stock,1,100" for i in range(6)]
    r = await client.post("/portfolios/import-all", files=_csv(rows, "portfolio_name,ticker,instrument_type,quantity,purchase_price"), headers=h)
    assert r.status_code == 200
    assert storage_service.count_portfolios(uid) <= settings.free_max_portfolios
    assert r.json()["errors"] >= 6 - settings.free_max_portfolios


async def test_import_all_rejects_bad_ticker_and_long_name(client) -> None:
    uid, h = await _free_user(client, "imp_bad")
    rows = ["P1,A/../x,stock,1,100", ("X" * 150) + ",SBER,stock,1,100"]
    r = await client.post("/portfolios/import-all", files=_csv(rows, "portfolio_name,ticker,instrument_type,quantity,purchase_price"), headers=h)
    assert r.json()["added"] == 0 and r.json()["errors"] == 2


async def test_csv_import_respects_item_limit(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_items_per_portfolio", 3)
    uid, h = await _free_user(client, "imp_items")
    pid = storage_service.get_portfolios(uid)[0]["id"] if storage_service.get_portfolios(uid) else storage_service.create_portfolio(uid, "P")
    rows = [f"T{i}X,stock,1,100" for i in range(5)]
    r = await client.post(f"/portfolios/{pid}/import", files=_csv(rows, "ticker,instrument_type,quantity,purchase_price"), headers=h)
    assert r.status_code == 200
    assert storage_service.count_items(pid) == 3
    assert r.json()["errors"] == 2


async def test_bulk_add_respects_item_limit(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_items_per_portfolio", 2)
    uid, h = await _free_user(client, "imp_bulk")
    pid = storage_service.get_portfolios(uid)[0]["id"] if storage_service.get_portfolios(uid) else storage_service.create_portfolio(uid, "P")
    payload = [{"ticker": f"T{i}X", "instrument_type": "stock", "quantity": 1, "purchase_price": 100} for i in range(3)]
    r = await client.post(f"/portfolios/{pid}/instruments/bulk", json=payload, headers=h)
    assert r.status_code == 400
