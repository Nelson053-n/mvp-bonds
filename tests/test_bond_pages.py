"""Tests for public bond SEO pages (/bond, /bond/{secid}, /sitemap-bonds.xml)."""

import time
from datetime import date, timedelta

import pytest

from app.api import bond_pages
from app.exceptions import PriceNotFoundError
from app.models import BondSnapshot
from app.services.moex_service import moex_service


def _make_snapshot(**overrides) -> BondSnapshot:
    today = date.today()
    defaults = dict(
        ticker="SU26238RMFS4",
        name="ОФЗ 26238",
        clean_price_percent=55.5,
        prev_close_percent=55.0,
        nominal=1000.0,
        coupon=35.4,
        coupon_period=182,
        coupon_rate=7.1,
        maturity_date=today + timedelta(days=3650),
        next_coupon_date=today + timedelta(days=30),
        aci=12.34,
        market_yield=14.5,
        company_rating="AAA",
        face_unit="SUR",
    )
    defaults.update(overrides)
    return BondSnapshot(**defaults)


_FAKE_BONDS = [
    {"ticker": "SU26238RMFS4", "name": "ОФЗ 26238", "price": 55.5, "market_yield": 14.5,
     "maturity": "2036-05-15", "board": "TQOB"},
    {"ticker": "SU26240RMFS0", "name": "ОФЗ 26240", "price": 60.1, "market_yield": 14.2,
     "maturity": "2036-07-30", "board": "TQOB"},
    {"ticker": "RU000A106K43", "name": "ТестКорп БО-1", "price": 98.0, "market_yield": 21.0,
     "maturity": "2028-01-01", "board": "TQCB"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Clear page caches and stub external data sources for every test."""
    bond_pages._page_cache.clear()
    bond_pages._notfound_cache.clear()
    bond_pages._catalog_cache = None
    bond_pages._sitemap_cache = None

    async def fake_catalog():
        return list(_FAKE_BONDS)

    monkeypatch.setattr(bond_pages, "_catalog_bonds", fake_catalog)
    yield


async def test_bond_page_renders(client, monkeypatch):
    async def fake_snapshot(secid):
        return _make_snapshot()

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/SU26238RMFS4")
    assert resp.status_code == 200
    assert "public" in resp.headers["cache-control"]
    html = resp.text
    assert "ОФЗ 26238" in html
    assert "SU26238RMFS4" in html
    assert "14,50%" in html                      # YTM
    assert "FAQPage" in html                     # JSON-LD
    assert "FinancialProduct" in html
    assert 'rel="canonical"' in html
    assert "/bond/SU26240RMFS0" in html          # related bond link
    assert "/app?auth=register" in html          # CTA


async def test_bond_page_cached(client, monkeypatch):
    calls = {"n": 0}

    async def fake_snapshot(secid):
        calls["n"] += 1
        return _make_snapshot()

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    assert (await client.get("/bond/SU26238RMFS4")).status_code == 200
    assert (await client.get("/bond/SU26238RMFS4")).status_code == 200
    assert calls["n"] == 1  # second hit served from page cache


async def test_bond_page_unknown_404(client, monkeypatch):
    async def fake_snapshot(secid):
        raise PriceNotFoundError(secid, "облигация")

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/RU000A0000XX")
    assert resp.status_code == 404
    assert "не найдена" in resp.text
    # negative-cached
    assert bond_pages._notfound_cache.get("RU000A0000XX")


async def test_bond_page_invalid_secid_404(client):
    resp = await client.get("/bond/__bad%20id__")
    assert resp.status_code == 404


async def test_bond_page_html_escapes_name(client, monkeypatch):
    async def fake_snapshot(secid):
        return _make_snapshot(name='<script>alert(1)</script>')

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/SU26238RMFS4")
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text


async def test_yield_to_offer_note(client, monkeypatch):
    async def fake_snapshot(secid):
        return _make_snapshot(
            market_yield=2293.0,
            offer_date=date.today() + timedelta(days=20),
        )

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/SU26238RMFS4")
    assert resp.status_code == 200
    assert "доходность к оферте" in resp.text
    assert "Доходность к оферте" in resp.text  # KPI label swapped


async def test_catalog_page(client):
    resp = await client.get("/bond")
    assert resp.status_code == 200
    html = resp.text
    assert "ОФЗ 26238" in html
    assert "ТестКорп БО-1" in html
    assert "/bond/RU000A106K43" in html
    # smart-lab-style controls: type chips, search, sortable headers
    assert 'data-board="TQOB"' in html
    assert 'id="cat-search"' in html
    assert 'data-col="3"' in html
    assert "Лет до погаш." in html


async def test_sitemap_bonds(client):
    resp = await client.get("/sitemap-bonds.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "https://bondai.ru/bond</loc>" in body
    assert "https://bondai.ru/bond/SU26238RMFS4</loc>" in body
    assert body.count("<url>") == 1 + len(_FAKE_BONDS)


async def test_robots_and_sitemap_reference_bonds(client):
    robots = (await client.get("/robots.txt")).text
    assert "Sitemap: https://bondai.ru/sitemap-bonds.xml" in robots
    sitemap = (await client.get("/sitemap.xml")).text
    assert "https://bondai.ru/bond</loc>" in sitemap
