"""Страницы разбора ОФЗ (/ofz, /ofz/{номер}, /sitemap-ofz.xml).

Данные — живые фикстуры MOEX от 21.10.2026 (tests/fixtures/moex). Чего в них нет,
синтезировано ЗДЕСЬ и помечено: годовая история кривой (в фикстуре один день —
остальные дни сдвигают его узлы на ±0.01 п.п. за день), график выплат
bondization для выпусков без фикстуры (полугодовой купон из справочника),
description для них же, поля линкера (facevalue/iricpiclose/beiclose) в истории
52002. Сеть не используется: `moex_service._fetch` подменён.
"""
import asyncio
import json
import re
import sys
import types
from html import unescape
from pathlib import Path

import pytest

from app.api import bond_pages, ofz_pages
from app.models import BondSnapshot
from app.services import ofz_analytics as oa
from app.services import ofz_commentary as oc
from app.services import ofz_history_service as ohs
from app.services.moex_service import moex_service
from app.services.storage_service import storage_service

FIX = Path(__file__).parent / "fixtures" / "moex"

# Подключение роутера в main.py делается отдельно; пока его там нет — подключаем здесь.
from app.main import app as _app  # noqa: E402
if not any(getattr(r, "path", None) == "/ofz" for r in _app.routes):
    _app.include_router(ofz_pages.router)


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ── данные ───────────────────────────────────────────────────────────────────

def _securities():
    d = _load("iss_tqob_securities.json")["securities"]
    out = []
    for r in d["data"]:
        row = {k.lower(): v for k, v in zip(d["columns"], r)}
        row["kind"] = ohs.classify_kind(row["bondtype"], row["faceunit"])
        out.append(row)
    return out


def _days():
    d = _load("ofz_days.json")
    cols = [c.lower() for c in d["columns"]]
    return {day: [dict(zip(cols, r)) for r in rows] for day, rows in d["days"].items()}


def _selection():
    secs = _securities()
    window = [r for rows in _days().values() for r in rows]
    chosen = set(oa.select_ofz(secs, window))
    out = []
    for s in secs:
        if s["secid"] in chosen:
            out.append(dict(s, number=ohs.ofz_number(s["secid"])))
    order = {sid: i for i, sid in enumerate(oa.select_ofz(secs, window))}
    return sorted(out, key=lambda s: order[s["secid"]])


SEL = _selection()
SEC = {s["number"]: s for s in SEL}


def _seed_db():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM ofz_daily")
        conn.execute("DELETE FROM ofz_curve_daily")
        conn.commit()
    nodes = [tuple(n) for n in _load("zcyc_2026-10-21.json")["nodes"]]
    days = _days()
    for i, (day, rows) in enumerate(sorted(days.items())):
        out = []
        for r in rows:
            r = dict(r)
            if r["secid"] == "SU52002RMFS1":  # синтез: в фикстуре истории нет этих полей
                r.update(facevalue=1778.0 + i * 0.02, iricpiclose=r["yieldclose"], beiclose=5.3 + i * 0.001)
            if r["secid"] == "SU29016RMFS1":
                r["yieldclose"] = 0.0         # ПК: MOEX отдаёт 0
            out.append(r)
        storage_service.upsert_ofz_daily(out)
        shift = (i - 19) * 0.01               # синтез: кривая «плавает»
        storage_service.upsert_ofz_curve(day, [(p, v + shift) for p, v in nodes])


@pytest.fixture(autouse=True)
def env(monkeypatch):
    _seed_db()
    for d in (ofz_pages._ofz_page_cache, ofz_pages._ofz_locks, ofz_pages._meta_cache, ofz_pages._meta_locks):
        d.clear()
    bond_pages._page_cache.clear()
    bond_pages._notfound_cache.clear()

    warm = sys.modules.get("app.services.ofz_warm")
    if warm is None:
        try:
            from app.services import ofz_warm as warm
        except ImportError:  # T7 ещё не в репозитории: интерфейс — один async get_selection()
            warm = types.ModuleType("app.services.ofz_warm")
            monkeypatch.setitem(sys.modules, "app.services.ofz_warm", warm)
            import app.services
            monkeypatch.setattr(app.services, "ofz_warm", warm, raising=False)

    state = {"sel": list(SEL), "sel_calls": 0}

    async def get_selection():
        state["sel_calls"] += 1
        return list(state["sel"])

    monkeypatch.setattr(warm, "get_selection", get_selection, raising=False)

    fetched = []
    desc_ov, bond_ov = {}, {}
    state.update(fetched=fetched, desc_ov=desc_ov, bond_ov=bond_ov)

    async def fake_fetch(url, source="moex_price"):
        fetched.append(url)
        m = re.search(r"/securities/(SU\d{5}RMFS\d)/(description|bondization)\.json", url)
        if not m:
            raise AssertionError(f"неожиданный сетевой запрос {url}")
        secid, what = m.groups()
        ov = (desc_ov if what == "description" else bond_ov).get(secid)
        if isinstance(ov, Exception):
            raise ov
        if ov is not None:
            return ov
        return _moex_payload(secid, what)

    monkeypatch.setattr(moex_service, "_fetch", fake_fetch)
    yield state
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM ofz_daily")
        conn.execute("DELETE FROM ofz_curve_daily")
        conn.commit()


def _moex_payload(secid, what):
    fixtures = {
        ("SU29016RMFS1", "description"): "iss_desc29016.json",
        ("SU46020RMFS2", "description"): "iss_desc46020.json",
        ("SU52002RMFS1", "description"): "iss_desc52002.json",
        ("SU29016RMFS1", "bondization"): "iss_bondiz29016.json",
        ("SU46020RMFS2", "bondization"): "iss_bondiz46020.json",
    }
    if (secid, what) in fixtures:
        return _load(fixtures[(secid, what)])
    sec = next(s for s in _securities() if s["secid"] == secid)
    if what == "description":
        rows = [("SECID", secid), ("SHORTNAME", sec["shortname"]), ("MATDATE", sec["matdate"]),
                ("ISSUEDATE", "2021-06-23"), ("INITIALFACEVALUE", "1000"), ("ISSUESIZE", str(sec["issuesize"] or 0)),
                ("FACEVALUE", "1000"), ("COUPONFREQUENCY", "2"), ("COUPONPERCENT", str(sec["couponpercent"] or 0))]
        if sec["kind"] == "float":
            rows += [("COUPON_BENCHMARK", "Ключевая ставка"), ("COUPON_BENCHMARK_SPREAD", "0.00")]
        return {"description": {"columns": ["name", "title", "value"], "data": [[k, k, v] for k, v in rows]}}
    # bondization синтезирован: купон из справочника каждые 182 дня от NEXTCOUPON + номинал в погашение
    from datetime import date, timedelta
    d = date.fromisoformat(sec["nextcoupon"])
    mat = date.fromisoformat(sec["matdate"])
    val = sec["couponvalue"] or 0
    rows = []
    while d <= mat:
        rows.append({"coupondate": d.isoformat(), "startdate": (d - timedelta(days=182)).isoformat(),
                     "value": val if sec["kind"] != "float" else None,
                     "valueprc": sec["couponpercent"] if sec["kind"] != "float" else None, "facevalue": 1000})
        d += timedelta(days=182)
    coupons = {"columns": list(rows[0]), "data": [list(r.values()) for r in rows]}
    amort = {"columns": ["amortdate", "value", "valueprc", "data_source"],
             "data": [[sec["matdate"], 1000, 100, "maturity"]]}
    return {"coupons": coupons, "amortizations": amort}


def _visible(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r'<p class="ofz-disc" data-disclaimer="1">.*?</p>', " ", html, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def _jsonld(html: str) -> list:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
    return [json.loads(b) for b in blocks]


# ── рендер каждого типа ──────────────────────────────────────────────────────

KIND_CASES = [("26238", "fixed"), ("29027", "float"), ("29016", "float"), ("52002", "linker"), ("46020", "amort")]


@pytest.mark.parametrize("num,kind", KIND_CASES)
async def test_renders_each_kind_and_texts_are_clean(client, num, kind):
    assert SEC[num]["kind"] == kind
    r = await client.get(f"/ofz/{num}")
    assert r.status_code == 200, r.text[:300]
    assert r.headers["cache-control"] == "public, max-age=300, stale-while-revalidate=86400"
    html = r.text
    assert f"<h1>ОФЗ {num}: разбор выпуска</h1>" in html
    assert "по итогам торгов 20.10.2026" in html
    assert f'<link rel="canonical" href="https://bondai.ru/ofz/{num}">' in html
    assert f'href="/bond/{SEC[num]["secid"]}"' in html           # ссылка на карточку, без дубля таблицы
    assert "<td>Тикер" not in html
    assert oc.find_forbidden(_visible(html)) == []               # тексты без рекомендательной лексики
    assert oc.disclaimer("20.10.2026 (итоги торгов)") in unescape(html)
    assert "datePublished" not in html


async def test_fixed_page_numbers_and_blocks(client):
    html = (await client.get("/ofz/26238")).text
    assert "16,23%" in html                                        # YTM из ofz_daily
    assert "Доходность (YTM)" in html and "Спред к кривой" in html
    assert "модифицированная 5,83" in html                         # 6,77 г. / (1 + 16,23%): freq=1, а не 2
    assert "−16 б.п." in html
    assert html.count("<svg") >= 4                                 # доходность, цена, кривая, спред + купоны
    for shift in ("−3 п.п.", "−2 п.п.", "−1 п.п.", "+1 п.п.", "+2 п.п.", "+3 п.п."):
        assert shift in html
    assert "Выпуски с близкой дюрацией" in html and "Ближайшие выплаты" in html
    assert "С учётом выпуклости" in html                           # потоки bondization прошли контроль цены
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    assert "цена 51,52%" in desc and "доходность 16,23%" in desc and "дюрация 6,77 г." in desc


async def test_neighbors_are_links_within_selection(client):
    html = (await client.get("/ofz/26238")).text
    block = html.split("Выпуски с близкой дюрацией")[1].split("<h2>")[0]
    links = re.findall(r'href="/ofz/(\d{5})"', block)
    assert 1 <= len(links) <= 4 and set(links) <= set(SEC) - {"26238"}


async def test_float_without_yield_has_no_ytm(client):
    html = (await client.get("/ofz/29016")).text
    vis = _visible(html)
    assert "Доходность (YTM)" not in vis and "Доходность к погашению за год" not in vis
    assert "не рассчитывает" in vis
    assert "Спред к кривой" not in vis
    assert "RUONIA" in vis                                         # бенчмарк из description (фикстура)
    assert "Последний купон" in vis


async def test_float_with_yield_shows_it(client):
    vis = _visible((await client.get("/ofz/29027")).text)
    assert "Доходность (YTM)" in vis and "расчёт MOEX" in vis
    assert "Спред к кривой" not in vis                              # спред только для ПД/АД


async def test_linker_block_without_spread(client):
    html = (await client.get("/ofz/52002")).text
    vis = _visible(html)
    assert "Индексация номинала" in vis and "Реальная доходность" in vis and "Вменённая инфляция" in vis
    assert "Индексированный номинал" in vis and "1 778" in vis
    assert not re.search(r"\b[Сс]пред\b", vis)                      # спред к кривой не показываем
    assert "Сценарии изменения доходности" not in vis
    assert "Положение на кривой" not in vis
    assert "кривой доходности обычных ОФЗ" in vis                   # объяснение, а не число


async def test_amort_block(client):
    vis = _visible((await client.get("/ofz/46020")).text)
    assert "График амортизации" in vis and "25%" in vis
    assert "Погашения номинала" in vis


# ── безопасность разметки ────────────────────────────────────────────────────

async def test_name_is_escaped(client, env):
    evil = "<script>alert(1)</script>"
    env["sel"] = [dict(s, shortname=evil) if s["number"] == "26238" else s for s in SEL]
    html = (await client.get("/ofz/26238")).text
    assert evil not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    _jsonld(html)                                                   # JSON-LD остаётся валидным
    raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
    assert all("<" not in b for b in raw)


async def test_jsonld(client):
    html = (await client.get("/ofz/26238")).text
    blocks = _jsonld(html)
    types_ = [b["@type"] for b in blocks]
    assert types_ == ["BreadcrumbList", "FinancialProduct", "FAQPage"]
    assert "Article" not in html
    bc = blocks[0]["itemListElement"]
    assert [i["name"] for i in bc] == ["Главная", "Облигации", "ОФЗ", "26238"]
    assert bc[3]["item"] == "https://bondai.ru/ofz/26238"
    assert blocks[1]["url"] == "https://bondai.ru/ofz/26238"
    assert len(blocks[2]["mainEntity"]) >= 3


async def test_descriptions_are_unique(client):
    a = (await client.get("/ofz/26238")).text
    b = (await client.get("/ofz/26240")).text
    da = re.search(r'<meta name="description" content="([^"]*)"', a).group(1)
    db = re.search(r'<meta name="description" content="([^"]*)"', b).group(1)
    assert da != db


async def test_tables_are_scrollable_not_page_wide(client):
    html = (await client.get("/ofz/26238")).text
    assert html.count('<table class="cat-table"') >= 2
    assert html.count('<div class="tbl-wrap"><table') == html.count('<table class="cat-table"')
    assert ".tbl-wrap{overflow-x:auto" in html


# ── маршрутизация ────────────────────────────────────────────────────────────

async def test_not_in_selection_is_404(client):
    r = await client.get("/ofz/99999")
    assert r.status_code == 404


async def test_selection_excludes_foreign_numbers(client, env):
    env["sel"] = [s for s in SEL if s["number"] != "26238"]
    assert (await client.get("/ofz/26238")).status_code == 404


@pytest.mark.parametrize("bad", ["abc", "1234", "123456", "2623a", "26238%20", "SU26238"])
async def test_invalid_number_404_without_io(client, env, monkeypatch, bad):
    def boom(*a, **k):
        raise AssertionError("обращение к БД")
    monkeypatch.setattr(ofz_pages, "_load_db", boom)
    r = await client.get(f"/ofz/{bad}")
    assert r.status_code == 404
    assert env["sel_calls"] == 0 and env["fetched"] == []


async def test_secid_redirects_to_number(client):
    r = await client.get("/ofz/SU26238RMFS4")
    assert r.status_code == 301 and r.headers["location"] == "/ofz/26238"
    assert (await client.get("/ofz/SU99999RMFS0")).status_code == 404


async def test_no_selection_gives_503_not_404(client, env):
    env["sel"] = []
    r = await client.get("/ofz/26238")
    assert r.status_code == 503 and r.headers["cache-control"] == "no-store"
    assert (await client.get("/ofz")).status_code == 503


async def test_no_history_in_db_is_503_and_not_cached(client):
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM ofz_daily WHERE secid = 'SU26238RMFS4'")
        conn.commit()
    r = await client.get("/ofz/26238")
    assert r.status_code == 503
    assert "26238" not in ofz_pages._ofz_page_cache


# ── кэш ──────────────────────────────────────────────────────────────────────

async def test_cached_second_request(client, monkeypatch, env):
    calls = []
    orig = ofz_pages._load_db
    monkeypatch.setattr(ofz_pages, "_load_db", lambda *a: (calls.append(1), orig(*a))[1])
    r1 = await client.get("/ofz/26238")
    n_fetch = len(env["fetched"])
    r2 = await client.get("/ofz/26238")
    assert r1.text == r2.text and len(calls) == 1 and len(env["fetched"]) == n_fetch == 2


async def test_concurrent_requests_build_once(client, monkeypatch, env):
    calls = []
    orig = ofz_pages._load_db
    monkeypatch.setattr(ofz_pages, "_load_db", lambda *a: (calls.append(1), orig(*a))[1])
    real = moex_service._fetch

    async def slow(url, source="moex_price"):
        await asyncio.sleep(0.05)
        return await real(url, source)

    monkeypatch.setattr(moex_service, "_fetch", slow)
    rs = await asyncio.gather(*[client.get("/ofz/26238") for _ in range(10)])
    assert [r.status_code for r in rs] == [200] * 10
    assert len(calls) == 1 and len(env["fetched"]) == 2


async def test_stale_copy_served_while_rebuilding(client, monkeypatch):
    await client.get("/ofz/26238")
    html, t, ttl = ofz_pages._ofz_page_cache["26238"]
    ofz_pages._ofz_page_cache["26238"] = (html, t - ttl - 10, ttl)   # протухла, но моложе суток
    gate = asyncio.Event()
    orig = ofz_pages._build_page

    async def slow_build(*a):
        await gate.wait()
        return await orig(*a)

    monkeypatch.setattr(ofz_pages, "_build_page", slow_build)
    first = asyncio.create_task(client.get("/ofz/26238"))
    await asyncio.sleep(0.05)
    second = await asyncio.wait_for(client.get("/ofz/26238"), timeout=2)   # не ждёт пересборку
    assert second.status_code == 200 and second.text == html
    gate.set()
    assert (await first).status_code == 200


async def test_degraded_page_when_moex_fails(client, env):
    env["desc_ov"]["SU26238RMFS4"] = RuntimeError("MOEX down")
    env["bond_ov"]["SU26238RMFS4"] = RuntimeError("MOEX down")
    r = await client.get("/ofz/26238")
    assert r.status_code == 200
    vis = _visible(r.text)
    assert "Ближайшие выплаты" not in vis and "Профиль выпуска" in vis
    assert "Линейное приближение" in vis                            # без потоков — только дюрация
    assert ofz_pages._ofz_page_cache["26238"][2] == ofz_pages._DEGRADED_TTL
    assert oc.find_forbidden(vis) == []


# ── хаб, sitemap, ссылки ─────────────────────────────────────────────────────

async def test_hub(client, env):
    r = await client.get("/ofz")
    assert r.status_code == 200 and env["fetched"] == []            # 0 запросов к MOEX
    html = r.text
    assert len(SEL) == 50
    assert len(set(re.findall(r'href="/ofz/(\d{5})"', html))) == 50
    assert "О разборах" in html and "по итогам торгов 20.10.2026" in html
    assert '<link rel="canonical" href="https://bondai.ru/ofz">' in html
    assert oc.find_forbidden(_visible(html)) == []
    types_ = [b["@type"] for b in _jsonld(html)]
    assert types_ == ["BreadcrumbList", "ItemList"]
    assert html.count('<div class="tbl-wrap"><table') == 1


async def test_sitemap_ofz(client):
    r = await client.get("/sitemap-ofz.xml")
    assert r.status_code == 200 and "xml" in r.headers["content-type"]
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    assert locs[0] == "https://bondai.ru/ofz"
    assert set(locs[1:]) == {f"https://bondai.ru/ofz/{s['number']}" for s in SEL} and len(locs) == 51
    assert "<lastmod>2026-10-20</lastmod>" in r.text


async def test_ofz_urls_and_all_public_urls(monkeypatch):
    async def fake_catalog():
        return []
    monkeypatch.setattr(bond_pages, "_catalog_bonds", fake_catalog)
    urls = await bond_pages.all_public_urls()
    assert "https://bondai.ru/ofz" in urls and "https://bondai.ru/ofz/26238" in urls
    assert len(await ofz_pages.ofz_urls()) == 51


def test_robots_and_llms_constants():
    assert ofz_pages.ROBOTS_SITEMAP_LINE == "Sitemap: https://bondai.ru/sitemap-ofz.xml\n"
    assert all(l.startswith("- [") and l.endswith("\n") and "https://bondai.ru/ofz" in l for l in ofz_pages.LLMS_TXT_LINES)


def _snap(secid, name="ОФЗ 26238", face_unit="SUR"):
    from datetime import date, timedelta
    return BondSnapshot(ticker=secid, name=name, clean_price_percent=55.5, prev_close_percent=55.0, nominal=1000.0,
                        coupon=35.4, coupon_period=182, coupon_rate=7.1,
                        maturity_date=date.today() + timedelta(days=3650), aci=1.0, market_yield=14.5,
                        company_rating="AAA", face_unit=face_unit)


async def test_bond_card_links_to_review_only_for_selected_ofz(client, monkeypatch):
    async def fake_catalog():
        return []
    monkeypatch.setattr(bond_pages, "_catalog_bonds", fake_catalog)

    async def fake_snapshot(secid, **kw):
        return _snap(secid)
    monkeypatch.setattr(moex_service, "get_bond_snapshot", fake_snapshot)
    html = (await client.get("/bond/SU26238RMFS4")).text
    assert '<a href="/ofz/26238">Разбор выпуска →</a>' in html
    cny = next(s for s in _securities() if s["faceunit"] == "CNY")["secid"]   # юаневая ОФЗ, не в отборе
    assert "/ofz/" not in (await client.get(f"/bond/{cny}")).text
    assert "/ofz/" not in (await client.get("/bond/RU000A106K43")).text        # не ОФЗ
    assert "/ofz/" not in (await client.get("/bond/SU26231RMFS7")).text        # ОФЗ вне отбора (26231 — без торгов)


async def test_warm_rebuilds_cached_page_bypassing_ttl(client, monkeypatch, env):
    calls = []
    orig = ofz_pages._load_db
    monkeypatch.setattr(ofz_pages, "_load_db", lambda *a: (calls.append(1), orig(*a))[1])
    await client.get("/ofz/26238")
    assert len(calls) == 1
    t1 = ofz_pages._ofz_page_cache["26238"][1]
    await asyncio.sleep(0.01)
    assert await ofz_pages.warm("26238") is True
    assert len(calls) == 2                      # в обход свежего кэша
    assert ofz_pages._ofz_page_cache["26238"][1] > t1   # копия действительно заменена
    r = await client.get("/ofz/26238")
    assert r.status_code == 200 and len(calls) == 2   # отдаётся прогретая копия
    assert await ofz_pages.warm("00000") is False     # вне отбора
