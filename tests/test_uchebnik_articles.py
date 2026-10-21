"""Tests for public articles /uchebnik/{doxodnost-ofz,nkd,oferta}."""
import html as htmllib
import json
import re

import pytest

SLUGS = ["doxodnost-ofz", "nkd", "oferta"]
PATHS = [f"/uchebnik/{s}" for s in SLUGS]


def _jsonld(text: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    return [json.loads(b) for b in blocks]


def _article_text(text: str) -> str:
    art = re.search(r"<article.*?</article>", text, re.S).group(0)
    art = re.sub(r"<style.*?</style>", "", art, flags=re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", art))


@pytest.mark.parametrize("path", PATHS)
async def test_article_basics(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "stale-while-revalidate" in resp.headers["cache-control"]
    text = resp.text
    assert len(re.findall(r"<h1[ >]", text)) == 1
    assert f'<link rel="canonical" href="https://bondai.ru{path}">' in text
    assert f'property="og:url" content="https://bondai.ru{path}"' in text
    assert 'property="og:image"' in text
    assert "107693104" in text  # Yandex.Metrika
    assert 'data-theme' in text  # theme toggle from shell
    assert "не является индивидуальной инвестиционной рекомендацией" in text
    assert 'href="/app?auth=register"' in text
    # no emoji
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", _article_text(text))
    words = len(re.findall(r"\S+", _article_text(text)))
    assert 900 <= words <= 1400, words


@pytest.mark.parametrize("path", PATHS)
async def test_article_head(client, path):
    resp = await client.head(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", PATHS)
async def test_article_jsonld_valid_and_faq_matches_page(client, path):
    text = (await client.get(path)).text
    blocks = _jsonld(text)
    types = [b["@type"] for b in blocks]
    assert "Article" in types and "FAQPage" in types
    article = next(b for b in blocks if b["@type"] == "Article")
    assert article["mainEntityOfPage"]["@id"] == f"https://bondai.ru{path}"
    faq = next(b for b in blocks if b["@type"] == "FAQPage")
    ld = [(q["name"], q["acceptedAnswer"]["text"]) for q in faq["mainEntity"]]
    assert 4 <= len(ld) <= 5
    visible = [
        (htmllib.unescape(q), htmllib.unescape(a))
        for q, a in re.findall(
            r'<div class="q">(.*?)</div><div class="a">(.*?)</div>', text, re.S)
    ]
    assert visible == ld


@pytest.mark.parametrize("path", PATHS)
async def test_article_internal_links_not_broken(client, path):
    from app.main import app

    text = (await client.get(path)).text
    ids = set(re.findall(r'\sid="([^"]+)"', text))
    hrefs = re.findall(r'<a [^>]*href="(/[^"]*)"', text)
    assert hrefs
    for href in hrefs:
        url, _, frag = href.partition("#")
        route_path = url.split("?")[0]
        if route_path:
            assert any(
                getattr(r, "path_regex", None) is not None and r.path_regex.match(route_path)
                for r in app.routes
            ), f"no route for {href}"
        if frag and not route_path:
            assert frag in ids, f"missing anchor {href}"
    # cross-links required by the brief
    for needed in ("/calc/nkd" if "nkd" in path else "/calc/ytm", "/bond", "/uchebnik"):
        assert f'href="{needed}"' in text
    others = [p for p in PATHS if p != path]
    for o in others:
        assert f'href="{o}"' in text


async def test_articles_in_sitemap(client):
    sitemap = (await client.get("/sitemap.xml")).text
    for p in PATHS:
        assert f"<loc>https://bondai.ru{p}</loc>" in sitemap
    assert sitemap.count("<lastmod>2026-10-21</lastmod>") >= 3


async def test_articles_in_indexnow_list():
    from app.api.bond_pages import all_public_urls

    urls = await all_public_urls()
    for p in PATHS:
        assert f"https://bondai.ru{p}" in urls
    assert len(urls) == len(set(urls))


async def test_uchebnik_links_to_articles_and_not_shadowed(client):
    resp = await client.get("/uchebnik")
    assert resp.status_code == 200
    assert "Учебник по облигациям" in resp.text
    assert len(re.findall(r"<h1[ >]", resp.text)) == 1
    for p in PATHS:
        assert f'href="{p}"' in resp.text


async def test_unknown_article_is_404(client):
    resp = await client.get("/uchebnik/net-takoj-stati")
    assert resp.status_code == 404


def test_example_numbers_match_calculator_definitions():
    """Numbers quoted in the articles, recomputed with the /calc formulas."""
    def ytm(dirty, nom, rate, freq, n, red=None):
        red = nom if red is None else red
        c = nom * rate / 100 / freq
        lo, hi = 1e-6, 10.0
        for _ in range(200):
            mid = (lo + hi) / 2
            pv = sum(c / (1 + mid) ** (i / freq) for i in range(1, n + 1)) + red / (1 + mid) ** (n / freq)
            lo, hi = (mid, hi) if pv > dirty else (lo, mid)
        return (lo + hi) / 2 * 100

    from app.api.uchebnik_articles import ARTICLES

    ofz = json.dumps(ARTICLES["doxodnost-ofz"], ensure_ascii=False)
    nkd = json.dumps(ARTICLES["nkd"], ensure_ascii=False)
    off = json.dumps(ARTICLES["oferta"], ensure_ascii=False)

    def fmt(x):
        return f"{x:.2f}".replace(".", ",")

    # article 1
    for price, expect in ((90, "17,02"), (95, "14,60"), (100, "12,36"), (105, "10,28"), (110, "8,34")):
        assert fmt(ytm(price * 10, 1000, 12, 2, 6)) == expect
        assert expect in ofz
    assert fmt(120 / 950 * 100) == "12,63" and "12,63" in ofz
    assert fmt(410 / 950 / 3 * 100) == "14,39" and "14,39" in ofz
    # article 2
    assert fmt(60 * 45 / 182) == "14,84" and "14,84" in nkd
    assert fmt(1000 * 0.12 * 45 / 365) == "14,79" and "14,79" in nkd
    assert fmt(985 + 60 * 45 / 182) == "999,84" and "999,84" in nkd
    assert fmt(60 * 181 / 182) == "59,67" and fmt(985 + 60 * 181 / 182) == "1044,67"
    assert fmt(985 + 60 * 181 / 182 - 60) == "984,67"
    assert fmt(60 * 1 / 182) == "0,33" and fmt(60 * 91 / 182) == "30,00"
    assert round(10 * round(985 + 60 * 45 / 182, 2), 2) == 9998.40
    # article 3
    assert fmt(ytm(1020, 1000, 12, 2, 3)) == "10,80" and "10,80" in off
    assert fmt(ytm(1020, 1000, 12, 2, 8)) == "11,69" and "11,69" in off
    assert fmt(ytm(1050, 1000, 12, 2, 4)) == "9,42" and "9,42" in off
    assert fmt(ytm(1050, 1000, 12, 2, 10)) == "10,97" and "10,97" in off
    assert round(10 / 990 * 365 / 10 * 100) == 37
