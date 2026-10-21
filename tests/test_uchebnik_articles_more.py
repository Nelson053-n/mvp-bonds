"""Tests for the second batch of /uchebnik/<slug> articles (ten topics)."""
import html as htmllib
import json
import re

import pytest

from app.api.uchebnik_articles_more import SLUGS

OLD_SLUGS = ["doxodnost-ofz", "nkd", "oferta"]
ALL_SLUGS = OLD_SLUGS + SLUGS
PATHS = [f"/uchebnik/{s}" for s in SLUGS]
ALL_PATHS = [f"/uchebnik/{s}" for s in ALL_SLUGS]
# Forbidden in the new articles: no tax statements at all (rates, benefits, accounts).
FORBIDDEN = re.compile(r"(?<![а-яё])(?:ндфл|налог|иис|льгот|вычет)")


def _jsonld(text: str) -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    return [json.loads(b) for b in blocks]


def _article_text(text: str) -> str:
    art = re.search(r"<article.*?</article>", text, re.S).group(0)
    art = re.sub(r"<style.*?</style>", "", art, flags=re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", art))


def test_ten_new_slugs():
    assert len(SLUGS) == 10 and len(set(SLUGS)) == 10
    assert not set(SLUGS) & set(OLD_SLUGS)


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
    assert 'property="og:type" content="article"' in text
    assert "107693104" in text
    assert "data-theme" in text
    assert "не является индивидуальной инвестиционной рекомендацией" in text
    assert 'href="/app?auth=register"' in text
    assert 'class="edu-answer"' in text and 'class="edu-toc"' in text
    assert len(re.findall(r"<h2[ >]", text)) >= 6
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", text)
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
    assert {"Article", "FAQPage", "BreadcrumbList"} <= set(types)
    article = next(b for b in blocks if b["@type"] == "Article")
    assert article["mainEntityOfPage"]["@id"] == f"https://bondai.ru{path}"
    crumbs = next(b for b in blocks if b["@type"] == "BreadcrumbList")
    assert crumbs["itemListElement"][-1]["item"] == f"https://bondai.ru{path}"
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
async def test_article_internal_links(client, path):
    from app.main import app

    text = (await client.get(path)).text
    art = re.search(r"<article.*?</article>", text, re.S).group(0)
    ids = set(re.findall(r'\sid="([^"]+)"', text))
    hrefs = re.findall(r'<a [^>]*href="(/[^"]*)"', art)
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
    # every /uchebnik/... link must be a real article of one of the two batches
    for href in set(hrefs):
        if href.startswith("/uchebnik/"):
            assert href in ALL_PATHS, href
    others = {h for h in hrefs if h in ALL_PATHS and h != path}
    assert len(others) >= 3, others
    # the "Читайте также" block alone carries at least 3 articles plus the hubs
    related = re.search(r'id="chitajte-takzhe".*?</section>', art, re.S).group(0)
    related_articles = {h for h in re.findall(r'href="([^"]+)"', related) if h in ALL_PATHS}
    assert len(related_articles) >= 3 and path not in related_articles
    for hub in ("/uchebnik", "/calc", "/bond"):
        assert f'href="{hub}"' in related


@pytest.mark.parametrize("path", PATHS)
async def test_no_tax_words_in_new_articles(client, path):
    text = _article_text((await client.get(path)).text).lower()
    jsonld = json.dumps(_jsonld((await client.get(path)).text), ensure_ascii=False).lower()
    assert not FORBIDDEN.search(text), FORBIDDEN.search(text).group(0)
    assert not FORBIDDEN.search(jsonld), FORBIDDEN.search(jsonld).group(0)
    # no concrete key-rate value ("ключевая ставка 16%") in the texts
    assert not re.search(r"ключев\w+ ставк\w+ (?:\w+ )?\d", text)


async def test_articles_in_sitemap(client):
    sitemap = (await client.get("/sitemap.xml")).text
    for p in ALL_PATHS:
        assert f"<loc>https://bondai.ru{p}</loc>" in sitemap
    for p in PATHS:
        block = re.search(rf"<url>\s*<loc>https://bondai.ru{p}</loc>(.*?)</url>", sitemap, re.S).group(1)
        assert "<lastmod>2026-10-21</lastmod>" in block
    assert len(re.findall(r"<loc>", sitemap)) == len(set(re.findall(r"<loc>(.*?)</loc>", sitemap)))


async def test_articles_in_llms_txt(client):
    txt = (await client.get("/llms.txt")).text
    for p in ALL_PATHS:
        assert f"(https://bondai.ru{p})" in txt


async def test_articles_in_indexnow_list():
    from app.api.bond_pages import all_public_urls

    urls = await all_public_urls()
    for p in ALL_PATHS:
        assert f"https://bondai.ru{p}" in urls
    assert len(urls) == len(set(urls))


async def test_uchebnik_links_to_new_articles_and_not_shadowed(client):
    resp = await client.get("/uchebnik")
    assert resp.status_code == 200
    assert len(re.findall(r"<h1[ >]", resp.text)) == 1
    for p in ALL_PATHS:
        assert f'href="{p}"' in resp.text
    # existing single-segment routes are untouched
    for p in ("/calc", "/calc/ytm", "/calc/nkd", "/bond"):
        assert (await client.get(p)).status_code == 200


async def test_old_articles_link_to_new_ones(client):
    for old in OLD_SLUGS:
        text = (await client.get(f"/uchebnik/{old}")).text
        assert sum(f'href="/uchebnik/{s}"' in text for s in SLUGS) >= 2


# ── numbers quoted in the texts, recomputed from definitions ────────────────

def _fmt(x, nd=2):
    return f"{x:.{nd}f}".replace(".", ",")


def _price(y, nom, rate, freq, n):
    c = nom * rate / 100 / freq
    return sum(c / (1 + y) ** (i / freq) for i in range(1, n + 1)) + nom / (1 + y) ** (n / freq)


def _ytm(dirty, nom, rate, freq, n):
    lo, hi = 1e-6, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _price(mid, nom, rate, freq, n) > dirty else (lo, mid)
    return (lo + hi) / 2


def _irr(cfs):
    lo, hi = -0.9, 10.0
    for _ in range(300):
        mid = (lo + hi) / 2
        pv = sum(cf / (1 + mid) ** t for t, cf in cfs)
        lo, hi = (mid, hi) if pv > 0 else (lo, mid)
    return mid


def _text(slug):
    from app.api.uchebnik_articles import ARTICLES
    return json.dumps(ARTICLES[slug], ensure_ascii=False).replace("&nbsp;", " ")


def _cell(slug, *vals):
    """Value must be present as a whole table cell (catches a distorted table number)."""
    t = _text(slug)
    for v in vals:
        assert f"<td>{v}</td>" in t, f"{slug}: cell {v!r} missing"


def _answer(slug, *vals):
    """Value must be quoted in the short answer at the top (and in the text)."""
    from app.api.uchebnik_articles import ARTICLES
    a = ARTICLES[slug]["answer"].replace(" ", " ")
    for v in vals:
        assert v in a, f"{slug}: {v!r} not in short answer"


def _has(slug, *nums):
    t = _text(slug)
    for n in nums:
        assert n in t, f"{slug}: {n!r} not in text"


def test_numbers_reinvestment():
    y0 = _ytm(1000, 1000, 12, 2, 6)
    assert _fmt(y0 * 100) == "12,36"
    rows = {0: ("360,00", "1 360,00", "10,79"), 4: ("378,49", "1 378,49", "11,29"),
            8: ("397,98", "1 397,98", "11,82"), 12: ("418,52", "1 418,52", "12,36"),
            16: ("440,16", "1 440,16", "12,93")}
    for r, (fv_s, tot_s, cagr_s) in rows.items():
        p = r / 200
        fv = 60 * (((1 + p) ** 6 - 1) / p if p else 6)
        assert _fmt(fv) == fv_s
        assert _fmt(1000 + fv) == tot_s.replace(" ", "")
        assert _fmt(((1000 + fv) / 1000) ** (1 / 3) * 100 - 100) == cagr_s
        _has("reinvestirovanie-kuponov", fv_s, tot_s, cagr_s)
        _cell("reinvestirovanie-kuponov", fv_s, tot_s, cagr_s + "%")
    assert _fmt(360 / 1000 / 3 * 100) == "12,00"
    assert _fmt((12.93 - 10.79)) == "2,14"
    assert _fmt(360 / 1360 * 100, 1) == "26,5"
    _has("reinvestirovanie-kuponov", "12,00%", "2,14 п.п.", "26,5%")
    _answer("reinvestirovanie-kuponov", "12,00%", "12,36%", "1 360 ₽", "10,79%", "12,93%")


def test_numbers_current_vs_ytm():
    for name, price, cp, n, cur, simple, ytm_s in (
            ("A", 1080, 14, 4, "12,96", "9,26", "9,74"), ("B", 880, 8, 8, "9,09", "12,50", "12,20")):
        assert _fmt(cp * 10 / price * 100) == cur
        assert _fmt((cp * 5 * n + 1000 - price) / price / (n / 2) * 100) == simple
        assert _fmt(_ytm(price, 1000, cp, 2, n) * 100) == ytm_s
        _has("tekushaya-i-k-pogasheniyu", cur + "%", simple + "%", ytm_s + "%")
        _cell("tekushaya-i-k-pogasheniyu", cur + "%", simple + "%", ytm_s + "%")
        _answer("tekushaya-i-k-pogasheniyu", cur + "%", ytm_s + "%")
    assert _fmt(_ytm(1000, 1000, 12, 2, 6) * 100) == "12,36" and _fmt(120 / 1000 * 100) == "12,00"


def test_numbers_duration():
    from app.api.bond_pages import _macaulay_duration

    y = _ytm(1000, 1000, 12, 2, 6)
    pvs = [(i / 2, 60 + (1000 if i == 6 else 0)) for i in range(1, 7)]
    rows = [(t, cf, cf / (1 + y) ** t) for t, cf in pvs]
    for t, cf, pv in rows:
        _has("dyuraciya", _fmt(pv), _fmt(t * pv))
        _cell("dyuraciya", _fmt(pv), _fmt(t * pv))
    total = sum(pv for _, _, pv in rows)
    wsum = sum(t * pv for t, _, pv in rows)
    assert _fmt(total) == "1000,00" and _fmt(wsum) == "2606,18"
    d = wsum / total
    dm = d / (1 + y)
    assert _fmt(d) == "2,61" and _fmt(dm) == "2,32"
    for dy, price_s, exact_s, approx_s in ((0.02, "955,15", "−4,49%", "−4,64%"),
                                           (0.01, "977,19", "−2,28%", "−2,32%"),
                                           (-0.01, "1023,60", "+2,36%", "+2,32%")):
        p = _price(y + dy, 1000, 12, 2, 6)
        assert _fmt(p) == price_s
        sign = lambda v: ("+" if v > 0 else "−") + _fmt(abs(v)) + "%"
        assert sign((p / 1000 - 1) * 100) == exact_s
        assert sign(-dm * dy * 100) == approx_s
        _has("dyuraciya", price_s, exact_s, approx_s)
        _cell("dyuraciya", price_s, exact_s, approx_s)
    # table by maturity
    for T, ds, ms in ((1, "0,97", "0,86"), (2, "1,84", "1,63"), (3, "2,61", "2,32"),
                      (4, "3,29", "2,93"), (5, "3,90", "3,47")):
        n = 2 * T
        w = sum(i / 2 * (60 + (1000 if i == n else 0)) / (1 + y) ** (i / 2) for i in range(1, n + 1))
        assert _fmt(w / 1000) == ds and _fmt(w / 1000 / (1 + y)) == ms
        _has("dyuraciya", ds, ms)
        _cell("dyuraciya", ds, ms)
    # the catalog uses an approximate version: 2.60 vs exact 2.61, "-4,6" in the +2pp column
    assert _fmt(_macaulay_duration(12, 2, 3, y * 100)) == "2,60"
    assert _fmt(dm * 2, 1) == "4,6"
    _has("dyuraciya", "2,60 года", "минус 4,6%")
    _answer("dyuraciya", "2,61 года", "2,32", "2,32%", "2,28%")
    _has("dyuraciya", "2,60 года", "минус 4,6%")


def test_numbers_default_example():
    flows = [(0, -10000), (1, 1600), (2, 9 * 160 + 9 * 1000 + 300)]
    assert 9 * 160 + 9 * 1000 == 10440 and flows[2][1] == 10740
    assert _fmt(_irr(flows) * 100) == "11,94"
    assert _fmt(_irr([(0, -10000), (1, 1600), (2, 1600 + 10000)]) * 100) == "16,00"
    assert _fmt(_irr([(0, -1000), (1, 120), (2, 1120)]) * 100) == "12,00"
    _has("rejtingi-vdo", "11,94%", "16,00%", "12,00%", "10 440", "10 740")
    _cell("rejtingi-vdo", "16,00%", "11,94%", "12,00%")
    _answer("rejtingi-vdo", "11,94%", "12,00%")
    _has("rejtingi-vdo", "10 440", "10 740")


def test_numbers_floater():
    scen = {"315": [12, 9, 6], "345": [10, 10, 10], "375": [8, 11, 14]}
    for total, base in scen.items():
        assert str(round(sum((b + 1.5) * 10 for b in base))) == total
        _has("flouteri", total)
        _cell("flouteri", total)
        _answer("flouteri", total + " ₽")
    assert 3 * 120 == 360
    assert _fmt(115 / 980 * 100) == "11,73"
    assert _fmt(_ytm(980, 1000, 11.5, 1, 3) * 100) == "12,34"
    assert _fmt(_ytm(1000, 1000, 11.5, 1, 3) * 100) == "11,50"
    _has("flouteri", "11,73%", "12,34%", "135 + 105 + 75", "95 + 125 + 155")
    _answer("flouteri", "360 ₽")
    _has("flouteri", "11,73%", "12,34%", "135 + 105 + 75", "95 + 125 + 155")


def test_numbers_amortization():
    rem, sched = 1000.0, []
    for i in range(1, 7):
        cp = rem * 0.12 / 2
        am = 250 if i >= 3 else 0
        sched.append((i, rem, cp, am))
        rem -= am
    assert [(round(s[1]), round(s[2]), s[3], round(s[2] + s[3])) for s in sched] == [
        (1000, 60, 0, 60), (1000, 60, 0, 60), (1000, 60, 250, 310),
        (750, 45, 250, 295), (500, 30, 250, 280), (250, 15, 250, 265)]
    assert sum(s[2] for s in sched) == 270 and 270 + 1000 == 1270 and 360 + 1000 == 1360

    def pv(yy):
        return sum((cp + am) / (1 + yy) ** (i / 2) for i, _, cp, am in sched)

    def ytm_s(p):
        lo, hi = 1e-6, 10.0
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if pv(mid) > p else (lo, mid)
        return mid * 100

    for price, a_s, b_s in ((950, "15,25", "14,60"), (1000, "12,36", "12,36"), (1050, "9,69", "10,28")):
        assert _fmt(ytm_s(price)) == a_s and _fmt(_ytm(price, 1000, 12, 2, 6) * 100) == b_s
        _has("amortizaciya", a_s + "%", b_s + "%")
        _cell("amortizaciya", a_s + "%", b_s + "%")
    y = 0.1236
    d = sum(i / 2 * (cp + am) / (1 + y) ** (i / 2) for i, _, cp, am in sched) / pv(y)
    assert _fmt(d) == "2,02"
    assert _fmt(_ytm(1050, 1000, 12, 2, 4) * 100) == "9,42"
    _has("amortizaciya", "2,02 года", "2,61 года", "9,42%", "10,28%")
    _answer("amortizaciya", "270 ₽", "360 ₽", "2,02 года", "2,61")
    _has("amortizaciya", "9,42%", "10,28%")


def test_numbers_ladder():
    y = 0.1236
    mods, drops = [], []
    for T in range(1, 6):
        n = 2 * T
        w = sum(i / 2 * (60 + (1000 if i == n else 0)) / (1 + y) ** (i / 2) for i in range(1, n + 1))
        mods.append(w / 1000 / (1 + y))
        drops.append((_price(y + 0.01, 1000, 12, 2, n) / 1000 - 1))
    assert [_fmt(m) for m in mods] == ["0,86", "1,63", "2,32", "2,93", "3,47"]
    assert [_fmt(-d * 100) for d in drops] == ["0,86", "1,61", "2,28", "2,87", "3,39"]
    assert [_fmt(-d * 200000 / 1000, 1) for d in drops] == ["1,7", "3,2", "4,6", "5,7", "6,8"]
    assert _fmt(sum(mods) / 5) == "2,24"
    assert _fmt(-sum(drops) / 5 * 100) == "2,20"
    assert _fmt(-sum(drops) / 5 * 1000, 1) == "22,0"
    assert _fmt(-drops[4] * 1000, 1) == "33,9"
    assert 5 * 200000 * 0.12 == 120000 and sum(range(1, 6)) / 5 == 3
    _has("lestnica", "2,24", "−2,20%", "22,0", "33,9", "3,39%", "120 000")
    _cell("lestnica", "0,86", "1,63", "2,32", "2,93", "3,47", "2,24", "−0,86%", "−1,61%", "−2,28%",
          "−2,87%", "−3,39%", "−2,20%", "1,7", "3,2", "4,6", "5,7", "6,8", "22,0")
    _answer("lestnica", "2,20%", "3,39%")


def test_numbers_deposit_comparison():
    y0 = _ytm(1000, 1000, 12, 2, 6)
    for dy, price_s, chg in ((0.02, "968,14", "−3,19%"), (0, "1000,00", "0,00%"), (-0.02, "1033,56", "+3,36%")):
        p = _price(y0 + dy, 1000, 12, 2, 4)
        assert _fmt(p) == price_s
        v = (p / 1000 - 1) * 100
        assert (("+" if v > 0.005 else "−" if v < -0.005 else "") + _fmt(abs(v)) + "%") == chg
        _has("obligacii-ili-vklad", price_s, chg)
        _cell("obligacii-ili-vklad", price_s, chg)
        if dy:
            _answer("obligacii-ili-vklad", price_s, chg)


def test_numbers_fx():
    cases = {"1,38": [11.6, 11.2, 10.8], "5,00": [12, 12, 12], "8,39": [12.4, 12.8, 13.2]}
    totals = {"1,38": 12480, "5,00": 13800, "8,39": 15120}
    for irr_s, rates in cases.items():
        cfs = [(0, -12000)] + [(t + 1, 50 * rates[t] + (1000 * rates[t] if t == 2 else 0)) for t in range(3)]
        assert _fmt(_irr(cfs) * 100) == irr_s
        assert round(sum(c for _, c in cfs[1:])) == totals[irr_s]
        _has("valyutnye-obligacii", irr_s + "%")
        _cell("valyutnye-obligacii", irr_s + "%", f"{totals[irr_s]:,}".replace(",", " "))
        _answer("valyutnye-obligacii", irr_s + "%")
    assert 50 * 11.6 == pytest.approx(580) and (50 + 1000) * 10.8 == pytest.approx(11340)
    _has("valyutnye-obligacii", "12 480", "13 800", "15 120", "11 340")


def test_numbers_card():
    aci = 60 * 45 / 182
    assert _fmt(aci) == "14,84" and _fmt(985 + aci) == "999,84"
    assert _fmt(10 * round(985 + aci, 2)) == "9998,40"
    assert 1000 * 0.12 / 2 == 60 and 10 * 120 == 1200
    assert _fmt(120 / 985 * 100) == "12,18"
    _has("kak-chitat-kartochku", "14,84", "999,84", "12,18%", "9 998,40", "1 200")
    _cell("kak-chitat-kartochku", "14,84 ₽", "999,84 ₽")
    _answer("kak-chitat-kartochku", "14,84 ₽", "999,84 ₽", "98,5%", "985 ₽")
    _has("kak-chitat-kartochku", "12,18%", "9 998,40", "1 200")
