"""Tests for public calculator pages (/calc, /calc/nkd, /calc/ytm)."""


async def test_calc_index(client):
    resp = await client.get("/calc")
    assert resp.status_code == 200
    assert "public" in resp.headers["cache-control"]
    assert "/calc/nkd" in resp.text
    assert "/calc/ytm" in resp.text


async def test_calc_nkd(client):
    resp = await client.get("/calc/nkd")
    assert resp.status_code == 200
    html = resp.text
    assert "Калькулятор НКД" in html
    assert "накопленный купонный доход" in html.lower()
    assert "FAQPage" in html          # JSON-LD
    assert 'id="n-rate"' in html      # form present
    assert "nkdByRate" in html        # JS present
    assert 'rel="canonical"' in html
    # explanatory inline-SVG scheme + social card + analytics
    assert 'class="edu-fig"' in html
    assert 'property="og:image"' in html
    assert "107693104" in html


async def test_calc_ytm(client):
    resp = await client.get("/calc/ytm")
    assert resp.status_code == 200
    html = resp.text
    assert "доходности облигаций" in html
    assert "YTM" in html
    assert "FAQPage" in html
    assert 'id="y-price"' in html
    assert "calcYtm" in html


async def test_sitemap_includes_calculators(client):
    sitemap = (await client.get("/sitemap.xml")).text
    assert "https://bondai.ru/calc</loc>" in sitemap
    assert "https://bondai.ru/calc/nkd</loc>" in sitemap
    assert "https://bondai.ru/calc/ytm</loc>" in sitemap
    llms = (await client.get("/llms.txt")).text
    assert "calc/nkd" in llms
    assert "calc/ytm" in llms
