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
    async def fake_snapshot(secid, **kwargs):
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
    # Social cards: og:image + twitter:card so shared links render with a preview.
    assert 'property="og:image" content="https://bondai.ru/og-image.png"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert 'name="twitter:image"' in html
    # Analytics: Yandex.Metrika on public SEO pages (same counter as the app).
    assert "107693104" in html
    assert "mc.yandex.ru/metrika/tag.js" in html


async def test_bond_page_cached(client, monkeypatch):
    calls = {"n": 0}

    async def fake_snapshot(secid, **kwargs):
        calls["n"] += 1
        return _make_snapshot()

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    assert (await client.get("/bond/SU26238RMFS4")).status_code == 200
    assert (await client.get("/bond/SU26238RMFS4")).status_code == 200
    assert calls["n"] == 1  # second hit served from page cache


async def test_bond_page_concurrent_requests_render_once(client, monkeypatch):
    """Пачка одновременных запросов на одну бумагу = один поход в MOEX.

    Регрессия 31.08.2026: краулер прислал 31 запрос на /bond/ за минуту, каждый
    промах кэша шёл в SmartLab (лимит 4 зап/с на процесс), очередь вылезла за
    таймаут nginx и все они получили 504. Лок держит один рендер на бумагу.
    """
    import asyncio

    calls = {"n": 0}

    async def slow_snapshot(secid, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.05)   # имитация похода в сеть
        return _make_snapshot()

    monkeypatch.setattr(moex_service, "get_bond_snapshot", slow_snapshot)
    results = await asyncio.gather(
        *(client.get("/bond/SU26238RMFS4") for _ in range(10))
    )
    assert all(r.status_code == 200 for r in results)
    assert calls["n"] == 1


async def test_bond_page_survives_rating_outage(client, monkeypatch):
    """Страница отдаётся, даже если рейтинг недоступен — он не обязателен."""
    async def fake_snapshot(secid, **kwargs):
        assert kwargs.get("rating_optional") is True
        return _make_snapshot(company_rating=None)

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/SU26238RMFS4")
    assert resp.status_code == 200
    assert "ОФЗ 26238" in resp.text


async def test_bond_page_unknown_404(client, monkeypatch):
    async def fake_snapshot(secid, **kwargs):
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
    async def fake_snapshot(secid, **kwargs):
        return _make_snapshot(name='<script>alert(1)</script>')

    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    resp = await client.get("/bond/SU26238RMFS4")
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text


async def test_yield_to_offer_note(client, monkeypatch):
    async def fake_snapshot(secid, **kwargs):
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
    assert 'title="Лет до погашения"' in html
    # social cards + analytics on the catalog page too
    assert 'property="og:image"' in html
    assert "107693104" in html


async def test_topbar_fits_narrow_screens(client):
    """The CTA must shrink below 480px or it widens the whole page.

    Regression: "Попробовать бесплатно" (~211px) next to the logo and the theme
    toggle did not fit a 360px viewport, so every public page had a horizontal
    scroll. Shared shell — checked on a catalog page and a calculator page.
    """
    for path in ("/bond", "/calc"):
        html = (await client.get(path)).text
        # both labels ship in the HTML; CSS picks one, so no JS and no SEO loss
        assert "Попробовать бесплатно" in html
        assert '<span class="cta-short">Начать</span>' in html
        assert ".cta-short{display:none}" in html
        narrow = html.split("@media(max-width:480px)")[1].split("@media")[0]
        assert ".cta-long{display:none}" in narrow
        assert ".cta-short{display:inline}" in narrow
        # long in-body CTAs and headings must not push the page sideways either
        assert ".cta-box .btn{white-space:normal" in narrow
        assert "h1,h2,h3{overflow-wrap:break-word}" in narrow


async def test_catalog_table_fits_page_width(client):
    """The 12-column table must not be clipped by the 920px article shell.

    Regression: .wrap capped the page at 920px while the table asked for
    1020px+, so the Рейтинг/Погашение/Оферта columns fell outside the visible
    area and looked truncated.
    """
    resp = await client.get("/bond")
    html = resp.text
    # catalog opts into the wide shell
    assert "body.catalog main.wrap{max-width:1320px}" in html
    assert "classList.add('catalog')" in html
    # name column is the elastic one that truncates, so numeric/rating columns keep their width
    assert ".cat-table td.nm{max-width:190px" in html
    assert '<td class="nm"' in html
    # narrow screens keep the scroll, but it is announced and the name column is frozen.
    # The freeze must cover the whole scrolling range: the table needs a 1080px
    # track, so the scroll only disappears at a 1130px viewport.
    assert 'class="scroll-hint"' in html
    assert "@media(max-width:1129px)" in html
    sticky_block = html.split("@media(max-width:1129px)")[1].split("@media")[0]
    assert "position:sticky" in sticky_block and "td.nm" in sticky_block


async def test_catalog_yield_map(client):
    resp = await client.get("/bond")
    assert resp.status_code == 200
    html = resp.text
    # yield map: canvas, group chips, embedded data points
    assert 'id="ymap"' in html
    assert 'id="map-chips"' in html
    assert "Карта доходности" in html
    assert 'id="map-data"' in html
    import json as _json
    payload = html.split('<script id="map-data" type="application/json">')[1].split("</script>")[0]
    points = _json.loads(payload)
    # OFZ bonds (group 0) and corp (group 1) from _FAKE_BONDS with positive duration
    groups = {p[4] for p in points}
    assert 0 in groups and 1 in groups
    assert all(len(p) == 7 for p in points)  # [secid, name, ytm, dur, group, years, rating_bucket]
    assert all(p[3] > 0 for p in points)  # duration
    # dohod-style extras: axis toggle, strategy presets, duration + rate-risk columns
    assert 'id="x-chips"' in html
    assert 'id="strat-chips"' in html
    assert "Дюрация" in html
    assert "При&nbsp;+2%" in html


def test_macaulay_duration():
    # zero-coupon: duration == maturity
    d = bond_pages._macaulay_duration(0, 2, 5.0, 10.0)
    assert d == pytest.approx(5.0, abs=0.3)
    # coupon bond: duration < maturity
    d = bond_pages._macaulay_duration(10.0, 2, 10.0, 15.0)
    assert d is not None and 3.0 < d < 8.0
    # invalid inputs
    assert bond_pages._macaulay_duration(10.0, 2, 0, 15.0) is None
    assert bond_pages._macaulay_duration(10.0, 2, 5.0, None) is None


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
