"""Public SEO pages for MOEX bonds: /bond/{secid}, /bond catalog, /sitemap-bonds.xml.

Server-rendered HTML built from the same MOEX data the app already fetches
(moex_service snapshots + the cached bonds list in app.api.bonds). Pages are
cached in-memory for _PAGE_TTL seconds and served with public Cache-Control,
so crawler traffic never hammers MOEX/SmartLab.
"""
import json
import logging
import re
import time
from datetime import date
from html import escape as e

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.exceptions import MOEXError
from app.models import BondSnapshot
from app.services.moex_service import moex_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-bonds"])

_BASE_URL = "https://bondai.ru"
_OG_IMAGE = "https://bondai.ru/og-image.png"
_SECID_RE = re.compile(r"^[A-Z0-9-]{4,24}$")

# Yandex.Metrika (counter 107693104) — same as landing/dashboard, so public SEO
# pages (/bond, /calc*) feed the same analytics. Plain string (not in the f-string)
# to avoid brace-escaping; CSP already allows mc.yandex.ru.
_METRIKA = """<script>(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};m[i].l=1*new Date();for(var j=0;j<document.scripts.length;j++){if(document.scripts[j].src===r){return;}}k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})(window,document,"script","https://mc.yandex.ru/metrika/tag.js?id=107693104","ym");ym(107693104,"init",{ssr:true,clickmap:true,trackLinks:true,accurateTrackBounce:true});</script><noscript><div><img src="https://mc.yandex.ru/watch/107693104" style="position:absolute;left:-9999px;" alt=""></div></noscript>"""

_PAGE_TTL = 900        # rendered bond page cache
_CATALOG_TTL = 3600    # rendered catalog page cache
_NOTFOUND_TTL = 600    # negative cache: unknown secids (protects MOEX from crawler junk)
_PAGE_CACHE_MAX = 800

_page_cache: dict[str, tuple[str, float]] = {}
_notfound_cache: dict[str, float] = {}
_catalog_cache: tuple[str, float] | None = None
_sitemap_cache: tuple[str, float] | None = None

# Short freshness so deploys propagate in minutes; SWR keeps repeat views instant
# while the browser revalidates in the background. Origin cost is covered by the
# in-memory page cache, so the short max-age doesn't add MOEX load.
_PUBLIC_CACHE = {"Cache-Control": "public, max-age=300, stale-while-revalidate=86400"}

_MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_CCY_SIGN = {"SUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥", "CHF": "₣"}


def _fmt_date(d: date | None) -> str | None:
    if d is None:
        return None
    return f"{d.day} {_MONTHS_GEN[d.month]} {d.year}"


def _fmt_money(v: float | None, digits: int = 2) -> str | None:
    if v is None:
        return None
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s


def _freq_text(period_days: int | None) -> str | None:
    if not period_days or period_days <= 0:
        return None
    if period_days <= 35:
        return "12 раз в год (ежемесячно)"
    if period_days <= 100:
        return "4 раза в год (ежеквартально)"
    if period_days <= 200:
        return "2 раза в год (раз в полгода)"
    return "1 раз в год"


def _rating_bucket(rating: str | None, board: str) -> int:
    """Credit-quality bucket for the yield map / strategy presets:
    0 = AAA/AA (incl. sovereign), 1 = A/BBB, 2 = BB and below, 3 = unrated."""
    if board == "TQOB":
        return 0
    if not rating:
        return 3
    r = rating.upper()
    if r.startswith("AA"):
        return 0
    if r.startswith("A") or r.startswith("BBB"):
        return 1
    return 2


def _macaulay_duration(coupon_pct: float | None, freq: int | None,
                       years: float, ytm_pct: float | None) -> float | None:
    """Approximate Macaulay duration (years) from coupon rate, payment frequency,
    time to maturity/offer and market yield. Good enough for the yield map."""
    if not years or years <= 0 or not ytm_pct or ytm_pct <= 0:
        return None
    f = freq if freq and freq > 0 else 2
    y = ytm_pct / 100.0
    n = max(1, round(years * f))
    c = (coupon_pct or 0.0) / 100.0 / f
    v = 1.0 / (1.0 + y / f)
    pv = 0.0
    wt = 0.0
    df = 1.0
    for k in range(1, n + 1):
        df *= v
        cf = c + (1.0 if k == n else 0.0)
        pv += cf * df
        wt += (k / f) * cf * df
    if pv <= 0:
        return None
    return wt / pv


async def _catalog_bonds() -> list[dict]:
    """All traded bonds (TQOB + TQCB) from the shared hourly cache in app.api.bonds."""
    from app.api import bonds as bonds_api
    try:
        return await bonds_api._get_bonds_cached()
    except Exception as exc:
        logger.warning("bond_pages: catalog fetch failed: %s", exc)
        return []


def _cache_put(secid: str, html: str) -> None:
    if len(_page_cache) >= _PAGE_CACHE_MAX:
        now = time.time()
        expired = [k for k, v in _page_cache.items() if now - v[1] > _PAGE_TTL]
        for k in expired:
            _page_cache.pop(k, None)
        if len(_page_cache) >= _PAGE_CACHE_MAX:
            for k in sorted(_page_cache, key=lambda k: _page_cache[k][1])[:100]:
                _page_cache.pop(k, None)
    _page_cache[secid] = (html, time.time())


# ── Shared page chrome ───────────────────────────────────────────────────────

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--blue-400:#60a5fa;--blue-500:#3b82f6;--blue-600:#2563eb;--indigo-500:#6366f1;
--green-400:#4ade80;--green-500:#22c55e;--red-400:#f87171;--yellow-400:#fbbf24;
--slate-300:#cbd5e1;--slate-400:#94a3b8;--slate-500:#64748b;--slate-600:#475569;
--slate-700:#334155;--slate-800:#1e293b;--slate-900:#0f172a;
--radius-sm:6px;--radius:8px;--radius-lg:12px;
--bg:#020817;--panel:#0f172a;--panel-2:#0a1326;--input-bg:rgba(2,8,23,.6);
--head:#fff;--text-strong:#e2e8f0;--text-soft:#cbd5e1;--text:#94a3b8;--muted:#64748b;--faint:#475569;
--line:rgba(148,163,184,.08);--line-2:rgba(148,163,184,.1);--line-3:rgba(148,163,184,.15);
--line-strong:rgba(148,163,184,.3);
--link:#60a5fa;--link-h:#3b82f6;--green:#4ade80;--red:#f87171;
--topbar-bg:rgba(2,8,23,.85);--hover-bg:rgba(255,255,255,.05);
--mesh:rgba(148,163,184,.04);--orb-op:1;
--note-bg:rgba(245,158,11,.08);--note-bd:rgba(245,158,11,.3);--note-tx:#fcd34d;
--pill-bg:rgba(96,165,250,.12);--pill-tx:#60a5fa;
--accent-bg:rgba(37,99,235,.07);--accent-bd:rgba(37,99,235,.2)}
html[data-theme=light]{--bg:#f6f8fb;--panel:#fff;--panel-2:#eef2f7;--input-bg:#fff;
--head:#0f172a;--text-strong:#1e293b;--text-soft:#334155;--text:#475569;--muted:#64748b;--faint:#94a3b8;
--line:rgba(15,23,42,.08);--line-2:rgba(15,23,42,.1);--line-3:rgba(15,23,42,.16);
--line-strong:rgba(15,23,42,.3);
--link:#2563eb;--link-h:#1d4ed8;--green:#15803d;--red:#dc2626;
--topbar-bg:rgba(255,255,255,.85);--hover-bg:rgba(15,23,42,.04);
--mesh:rgba(15,23,42,.04);--orb-op:.35;
--note-bg:rgba(245,158,11,.1);--note-bd:rgba(217,119,6,.35);--note-tx:#92400e;
--pill-bg:rgba(37,99,235,.1);--pill-tx:#2563eb;
--accent-bg:rgba(37,99,235,.06);--accent-bd:rgba(37,99,235,.25)}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);
line-height:1.65;-webkit-font-smoothing:antialiased;min-height:100vh;display:flex;flex-direction:column;
transition:background .2s,color .2s}
.mesh-bg{position:fixed;inset:0;pointer-events:none;z-index:0;
background-image:linear-gradient(var(--mesh) 1px,transparent 1px),
linear-gradient(90deg,var(--mesh) 1px,transparent 1px);background-size:44px 44px}
.orbs{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden;opacity:var(--orb-op)}
.orb{position:absolute;border-radius:50%;filter:blur(80px);will-change:transform}
.orb-1{width:700px;height:700px;top:-200px;left:-150px;
background:radial-gradient(circle,rgba(37,99,235,.45) 0%,rgba(37,99,235,.14) 45%,transparent 70%);
animation:orbDrift1 13s ease-in-out infinite}
.orb-2{width:600px;height:600px;bottom:-120px;right:-120px;
background:radial-gradient(circle,rgba(99,102,241,.4) 0%,rgba(99,102,241,.12) 45%,transparent 70%);
animation:orbDrift2 17s ease-in-out infinite}
@keyframes orbDrift1{0%,100%{transform:translate(0,0) scale(1)}
33%{transform:translate(50px,40px) scale(1.06)}66%{transform:translate(-30px,60px) scale(.96)}}
@keyframes orbDrift2{0%,100%{transform:translate(0,0) scale(1)}
40%{transform:translate(-60px,-40px) scale(1.08)}70%{transform:translate(40px,-15px) scale(.94)}}
@media(prefers-reduced-motion:reduce){.orb{animation:none}}
.topbar,main.wrap,footer{position:relative;z-index:1}
a{color:var(--link);text-decoration:none;transition:color .15s}
a:hover{color:var(--link-h)}
.wrap{max-width:920px;margin:0 auto;padding:0 24px;width:100%}
.topbar{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:12px;
padding:0 32px;height:56px;background:var(--topbar-bg);
backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
border-bottom:1px solid var(--line)}
.topbar-logo{display:flex;align-items:center;gap:10px;flex-shrink:0}
.logo-sq{width:30px;height:30px;background:linear-gradient(135deg,#2563eb,#4f46e5);
border-radius:var(--radius-sm);display:flex;align-items:center;justify-content:center;
font-weight:800;font-size:15px;color:#fff;letter-spacing:-.5px;box-shadow:0 0 14px rgba(37,99,235,.45)}
.logo-text{font-weight:700;font-size:15px;color:var(--head);letter-spacing:-.2px}
.topbar-spacer{flex:1}
.topbar-nav{display:flex;align-items:center;gap:18px}
.topbar-nav a.nav-link{font-size:13px;font-weight:600;color:var(--muted)}
.topbar-nav a.nav-link:hover{color:var(--link)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
font-family:inherit;font-size:13px;font-weight:600;border-radius:var(--radius-sm);
padding:7px 14px;cursor:pointer;transition:all .15s ease;text-decoration:none;
border:1.5px solid transparent;white-space:nowrap}
.btn-ghost{background:transparent;border-color:var(--line-3);color:var(--text-soft)}
.btn-ghost:hover{border-color:var(--line-strong);color:var(--head);background:var(--hover-bg)}
.theme-btn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
background:transparent;border:1.5px solid var(--line-3);border-radius:var(--radius-sm);
color:var(--text-soft);cursor:pointer;transition:all .15s;flex-shrink:0;padding:0}
.theme-btn:hover{border-color:var(--line-strong);color:var(--head);background:var(--hover-bg)}
.theme-btn .ic-moon{display:none}
html[data-theme=light] .theme-btn .ic-moon{display:block}
html[data-theme=light] .theme-btn .ic-sun{display:none}
.btn-primary{background:linear-gradient(135deg,#2563eb,#4f46e5);border-color:transparent;
color:#fff!important;box-shadow:0 0 20px rgba(37,99,235,.35)}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 24px rgba(37,99,235,.55);filter:brightness(1.08)}
main.wrap{flex:1;padding-top:36px;padding-bottom:72px}
.crumbs{font-size:13px;color:var(--faint);margin:0 0 22px}
.crumbs a{color:var(--muted)}
h1{font-size:clamp(26px,4.5vw,36px);font-weight:800;color:var(--head);letter-spacing:-.8px;
line-height:1.15;margin:0 0 10px}
.sub{font-size:15px;color:var(--text);margin-bottom:28px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:26px}
.kpi{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius-lg);padding:14px 16px}
.kpi-label{font-size:12px;color:var(--muted);margin-bottom:4px}
.kpi-value{font-size:19px;font-weight:700;color:var(--head);white-space:nowrap}
.kpi-value.green{color:var(--green)}.kpi-value.red{color:var(--red)}
.kpi-sub{font-size:12px;color:var(--muted);margin-top:2px}
h2{font-size:clamp(19px,3vw,24px);font-weight:800;color:var(--head);letter-spacing:-.4px;
margin:36px 0 16px;padding-bottom:10px;border-bottom:1px solid var(--line-2)}
p{font-size:15px;color:var(--text);line-height:1.8;margin-bottom:14px}
p strong,p b{color:var(--text-soft)}
table.params{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:8px}
table.params td{padding:10px 12px;border-bottom:1px solid var(--line)}
table.params td:first-child{color:var(--muted);width:46%}
table.params td:last-child{color:var(--text-strong);font-weight:500}
.note{background:var(--note-bg);border:1px solid var(--note-bd);
border-radius:var(--radius);padding:12px 16px;font-size:14px;color:var(--note-tx);margin:14px 0}
.faq-item{padding:16px 0;border-bottom:1px solid var(--line)}
.faq-item:last-of-type{border-bottom:none}
.faq-item h3{font-size:16px;font-weight:700;color:var(--head);margin-bottom:8px}
.faq-item p{font-size:14px;color:var(--text);line-height:1.75;margin:0}
.related{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.rel-card{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius);
padding:12px 14px;display:block;transition:border-color .15s}
.rel-card:hover{border-color:var(--line-strong)}
.rel-name{font-size:13px;font-weight:600;color:var(--text-strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rel-meta{font-size:12px;color:var(--muted);margin-top:3px}
.rel-meta b{color:var(--green);font-weight:600}
.cta-box{background:linear-gradient(135deg,rgba(37,99,235,.14),rgba(99,102,241,.1));
border:1px solid rgba(37,99,235,.25);border-radius:var(--radius-lg);padding:28px 24px;margin:44px 0;text-align:center}
.cta-box h2{font-size:22px;border:none;padding:0;margin:0 0 8px}
.cta-box p{font-size:15px;color:var(--text);margin-bottom:18px}
.cta-box .btn{font-size:15px;font-weight:700;padding:11px 28px;border-radius:var(--radius)}
footer{border-top:1px solid var(--line);padding:24px 32px;text-align:center}
footer p{font-size:12px;color:var(--faint);line-height:1.8;margin-bottom:4px}
footer a{color:var(--muted)}
footer a:hover{color:var(--link)}
.footer-sep{margin:0 6px;color:var(--faint)}
.cat-table{width:100%;border-collapse:collapse;font-size:14px}
.cat-table th{text-align:left;padding:8px 10px;color:var(--muted);font-size:12px;
border-bottom:1px solid var(--line-3)}
.cat-table td{padding:8px 10px;border-bottom:1px solid var(--line)}
.cat-table td.num{text-align:right;white-space:nowrap}
@media(max-width:680px){.topbar{padding:0 16px}.topbar-nav{gap:10px}
.topbar-nav a.nav-link{display:none}.btn-ghost{display:none}}
"""


def _page_shell(title: str, description: str, canonical: str, jsonld_blocks: list[dict],
                body: str, og_type: str = "website") -> str:
    # <-escape "<" so data can never break out of the <script> block (XSS via JSON-LD)
    jsonld = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, ensure_ascii=False).replace("<", "\\u003c")
        + "</script>"
        for b in jsonld_blocks
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script>(function(){{try{{if(localStorage.getItem('bondai_theme')==='light')document.documentElement.setAttribute('data-theme','light');}}catch(e){{}}}})();</script>
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:image" content="{_OG_IMAGE}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="ru_RU">
<meta property="og:site_name" content="Bond AI">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(description)}">
<meta name="twitter:image" content="{_OG_IMAGE}">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%232563eb'/><text x='16' y='23' font-family='Inter,Arial,sans-serif' font-size='20' font-weight='700' fill='white' text-anchor='middle'>B</text></svg>"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
{jsonld}
<style>{_CSS}</style>
</head>
<body>
<div class="mesh-bg"></div>
<div class="orbs"><div class="orb orb-1"></div><div class="orb orb-2"></div></div>
<header class="topbar">
  <a href="/" class="topbar-logo"><div class="logo-sq">B</div><span class="logo-text">Bond AI</span></a>
  <div class="topbar-spacer"></div>
  <nav class="topbar-nav">
    <a class="nav-link" href="/bond">Облигации</a>
    <a class="nav-link" href="/calc">Калькуляторы</a>
    <a class="nav-link" href="/uchebnik">Учебник</a>
    <button id="theme-toggle" class="theme-btn" aria-label="Переключить тему" title="Светлая / тёмная тема">
      <svg class="ic-sun" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
      <svg class="ic-moon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    <a id="nav-login" class="btn btn-ghost" href="/app?auth=login">Войти</a>
    <a id="nav-register" class="btn btn-primary" href="/app?auth=register">Попробовать бесплатно</a>
    <a id="nav-portfolio" class="btn btn-primary" href="/app" style="display:none;">Мои портфели &rarr;</a>
  </nav>
</header>
<script>
  // Public catalog renders without auth context; if a token is in localStorage,
  // swap the login/register CTAs for a "Мои портфели" link back to the app.
  (function(){{try{{
    if(!localStorage.getItem('mvp_auth_token'))return;
    var l=document.getElementById('nav-login'),r=document.getElementById('nav-register'),p=document.getElementById('nav-portfolio');
    if(l)l.style.display='none'; if(r)r.style.display='none'; if(p)p.style.display='inline-flex';
  }}catch(e){{}}}})();
</script>
<main class="wrap">
{body}
</main>
<footer>
<p>&copy; 2025 Bond AI<span class="footer-sep">&middot;</span><a href="/bond">Облигации</a><span class="footer-sep">&middot;</span><a href="/calc">Калькуляторы</a><span class="footer-sep">&middot;</span><a href="/uchebnik">Учебник по облигациям</a><span class="footer-sep">&middot;</span><a href="/privacy">Конфиденциальность</a><span class="footer-sep">&middot;</span><a href="/terms">Условия использования</a></p>
<p>Данные предоставлены Московской биржей. Информация носит справочный характер и не является индивидуальной инвестиционной рекомендацией.</p>
<p><a href="https://tbank.ru/baf/47BkLWQ33GF" target="_blank" rel="noopener" style="color:var(--link);">Откройте счёт в Т-Инвестициях и получите акции на 2000 &#8381; &rarr;</a></p>
</footer>
<script>(function(){{var b=document.getElementById('theme-toggle');if(!b)return;
b.addEventListener('click',function(){{var h=document.documentElement;
var light=h.getAttribute('data-theme')==='light';
if(light)h.removeAttribute('data-theme');else h.setAttribute('data-theme','light');
try{{localStorage.setItem('bondai_theme',light?'dark':'light');}}catch(e){{}}
window.dispatchEvent(new Event('bondai-theme'));}});}})();</script>
{_METRIKA}
</body>
</html>"""


# ── Bond page ────────────────────────────────────────────────────────────────

def _bond_kind(secid: str, board: str | None) -> str:
    if board == "TQOB" or secid.startswith("SU"):
        return "ОФЗ"
    return "корпоративная облигация"


def _render_bond_page(s: BondSnapshot, board: str | None, related: list[dict]) -> str:
    secid = s.ticker
    url = f"{_BASE_URL}/bond/{secid}"
    kind = _bond_kind(secid, board)
    ccy = _CCY_SIGN.get(s.face_unit, s.face_unit)
    today = date.today()

    price_rub = (s.clean_price_percent / 100.0 * s.nominal) if s.nominal else None
    day_delta = (
        s.clean_price_percent - s.prev_close_percent
        if s.prev_close_percent is not None else None
    )
    cur_yield = (
        round(s.coupon_rate / s.clean_price_percent * 100, 2)
        if s.coupon_rate and s.clean_price_percent else None
    )
    ytm = s.market_yield
    # YTM > 100% with a near put date is yield-to-offer, not YTM — explain it.
    near_offer = None
    if ytm and ytm > 100:
        for d in (s.offer_date, s.buyback_date):
            if d and d >= today and (near_offer is None or d < near_offer):
                near_offer = d

    days_to_mat = (s.maturity_date - today).days if s.maturity_date and s.maturity_date >= today else None

    # ── Auto-generated unique description (also used for meta/JSON-LD) ──
    desc_bits = [f"«{s.name}» — {kind}, торгуется на Московской бирже под тикером {secid}."]
    desc_bits.append(f"Текущая цена — {_fmt_money(s.clean_price_percent)}% от номинала"
                     + (f" ({_fmt_money(price_rub)} ₽)" if price_rub else "") + ".")
    if ytm and not near_offer:
        desc_bits.append(f"Доходность к погашению (YTM) — {_fmt_money(ytm)}% годовых.")
    if s.coupon and s.coupon_rate:
        freq = _freq_text(s.coupon_period)
        desc_bits.append(
            f"Купон — {_fmt_money(s.coupon)} ₽ (ставка {_fmt_money(s.coupon_rate)}% годовых"
            + (f", выплаты {freq}" if freq else "") + ")."
        )
    if s.next_coupon_date and s.next_coupon_date >= today:
        desc_bits.append(f"Следующая выплата купона — {_fmt_date(s.next_coupon_date)}.")
    if s.maturity_date:
        desc_bits.append(f"Дата погашения — {_fmt_date(s.maturity_date)}.")
    if s.offer_date and s.offer_date >= today:
        desc_bits.append(f"Ближайшая оферта — {_fmt_date(s.offer_date)}.")
    description = " ".join(desc_bits)

    meta_desc = (
        f"{s.name} ({secid}): цена {_fmt_money(s.clean_price_percent)}%"
        + (f", доходность {_fmt_money(ytm)}%" if ytm and not near_offer else "")
        + (f", купон {_fmt_money(s.coupon_rate)}%" if s.coupon_rate else "")
        + (f", погашение {_fmt_date(s.maturity_date)}" if s.maturity_date else "")
        + ". Купоны, оферта, рейтинг и параметры облигации — данные MOEX."
    )
    title = (
        f"{s.name} ({secid}) — цена, доходность"
        + (f" {_fmt_money(ytm)}%" if ytm and not near_offer else "")
        + ", купоны и погашение | Bond AI"
    )

    # ── KPI cards ──
    kpis: list[str] = []
    day_html = ""
    if day_delta is not None and abs(day_delta) >= 0.005:
        cls = "green" if day_delta > 0 else "red"
        day_html = f'<div class="kpi-sub"><span class="kpi-value {cls}" style="font-size:12px">{"+" if day_delta > 0 else ""}{_fmt_money(day_delta)} п.п. за день</span></div>'
    kpis.append(f'<div class="kpi"><div class="kpi-label">Цена</div><div class="kpi-value">{_fmt_money(s.clean_price_percent)}%</div>'
                + (f'<div class="kpi-sub">{_fmt_money(price_rub)} ₽</div>' if price_rub else "") + day_html + "</div>")
    if ytm:
        ytm_label = "Доходность к оферте" if near_offer else "Доходность (YTM)"
        kpis.append(f'<div class="kpi"><div class="kpi-label">{ytm_label}</div><div class="kpi-value green">{_fmt_money(ytm)}%</div></div>')
    if s.coupon_rate:
        kpis.append(f'<div class="kpi"><div class="kpi-label">Ставка купона</div><div class="kpi-value">{_fmt_money(s.coupon_rate)}%</div>'
                    + (f'<div class="kpi-sub">{_fmt_money(s.coupon)} ₽ за выплату</div>' if s.coupon else "") + "</div>")
    if cur_yield:
        kpis.append(f'<div class="kpi"><div class="kpi-label">Текущая доходность</div><div class="kpi-value">{_fmt_money(cur_yield)}%</div></div>')
    if s.company_rating:
        kpis.append(f'<div class="kpi"><div class="kpi-label">Кредитный рейтинг</div><div class="kpi-value">{e(s.company_rating)}</div></div>')
    if s.aci is not None:
        kpis.append(f'<div class="kpi"><div class="kpi-label">НКД</div><div class="kpi-value">{_fmt_money(s.aci)} ₽</div></div>')

    # ── Params table ──
    rows: list[tuple[str, str]] = [("Тикер (ISIN/SECID)", secid), ("Тип", kind.capitalize())]
    if s.nominal:
        nom = f"{_fmt_money(s.nominal)} ₽"
        if s.face_unit not in ("SUR", "RUB"):
            nom += f" (номинал в {e(s.face_unit)} {ccy}, пересчёт по курсу {_fmt_money(s.fx_rate, 4)})"
        rows.append(("Номинал", nom))
    if s.coupon:
        rows.append(("Размер купона", f"{_fmt_money(s.coupon)} ₽"))
    if s.coupon_rate:
        rows.append(("Ставка купона", f"{_fmt_money(s.coupon_rate)}% годовых" + (" (флоатер — ставка переменная)" if s.is_floater else "")))
    freq = _freq_text(s.coupon_period)
    if freq:
        rows.append(("Периодичность выплат", freq))
    if s.next_coupon_date and s.next_coupon_date >= today:
        rows.append(("Следующий купон", _fmt_date(s.next_coupon_date)))
    if s.maturity_date:
        mat = _fmt_date(s.maturity_date)
        if days_to_mat is not None:
            mat += f" (через {days_to_mat} дн.)"
        rows.append(("Дата погашения", mat))
    if s.offer_date and s.offer_date >= today:
        rows.append(("Оферта", _fmt_date(s.offer_date)))
    if s.aci is not None:
        rows.append(("НКД (накопленный купонный доход)", f"{_fmt_money(s.aci)} ₽"))
    if price_rub and s.aci is not None:
        rows.append(("Полная цена (с НКД)", f"{_fmt_money(price_rub + s.aci)} ₽"))
    if s.company_rating:
        rows.append(("Кредитный рейтинг", s.company_rating))
    rows.append(("Доступна неквалифицированным инвесторам", "Нет, только для квалов" if s.is_qual else "Да"))
    params_html = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in rows)

    # ── Notes ──
    notes = ""
    if near_offer:
        notes += (f'<div class="note">⚠️ Показанная доходность {_fmt_money(ytm)}% — это доходность к оферте '
                  f'{_fmt_date(near_offer)}, а не к погашению. Такие значения возникают у бумаг с близкой датой '
                  f'выкупа и не означают реальную годовую доходность. <a href="/uchebnik">Что такое оферта →</a></div>')
    if s.is_floater:
        notes += ('<div class="note">ℹ️ Это флоатер: купон привязан к ключевой ставке ЦБ или RUONIA и меняется '
                  'вместе с ней. Указанная ставка купона — последняя известная. <a href="/uchebnik">Подробнее о флоатерах →</a></div>')
    if s.is_qual:
        notes += '<div class="note">🔒 Бумага доступна только квалифицированным инвесторам.</div>'

    # ── FAQ (visible + JSON-LD share the same content) ──
    faq: list[tuple[str, str]] = []
    if ytm:
        a = (f"Доходность к оферте {_fmt_date(near_offer)} составляет {_fmt_money(ytm)}% годовых."
             if near_offer else
             f"Доходность к погашению (YTM) составляет {_fmt_money(ytm)}% годовых при цене {_fmt_money(s.clean_price_percent)}% от номинала.")
        if cur_yield:
            a += f" Текущая купонная доходность — {_fmt_money(cur_yield)}% годовых."
        faq.append((f"Какая доходность у облигации {s.name}?", a))
    if s.next_coupon_date and s.next_coupon_date >= today and s.coupon:
        faq.append((f"Когда следующий купон по {s.name}?",
                    f"Следующая выплата купона — {_fmt_date(s.next_coupon_date)}, размер купона {_fmt_money(s.coupon)} ₽ на одну облигацию."))
    if s.maturity_date:
        a = f"Дата погашения — {_fmt_date(s.maturity_date)}."
        if s.offer_date and s.offer_date >= today:
            a += f" Ближайшая оферта (право досрочного выкупа) — {_fmt_date(s.offer_date)}."
        faq.append((f"Когда погашение облигации {s.name}?", a))
    if price_rub:
        a = f"Текущая цена — {_fmt_money(s.clean_price_percent)}% от номинала, то есть {_fmt_money(price_rub)} ₽."
        if s.aci is not None:
            a += f" С учётом НКД ({_fmt_money(s.aci)} ₽) покупка одной облигации обойдётся примерно в {_fmt_money(price_rub + s.aci)} ₽."
        faq.append((f"Сколько стоит облигация {s.name}?", a))
    faq_html = "".join(
        f'<div class="faq-item"><h3>{e(q)}</h3><p>{e(a)}</p></div>' for q, a in faq
    )

    # ── Related bonds ──
    related_html = ""
    if related:
        cards = []
        for b in related:
            meta = []
            if b.get("market_yield"):
                meta.append(f"<b>{_fmt_money(b['market_yield'])}%</b>")
            if b.get("maturity"):
                meta.append(f"до {e(str(b['maturity'])[:4])}")
            cards.append(
                f'<a class="rel-card" href="/bond/{e(b["ticker"])}">'
                f'<div class="rel-name">{e(b["name"])}</div>'
                f'<div class="rel-meta">{" · ".join(meta)}</div></a>'
            )
        related_html = f'<h2>Похожие облигации</h2><div class="related">{"".join(cards)}</div>'

    # ── JSON-LD ──
    jsonld: list[dict] = [
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Главная", "item": _BASE_URL + "/"},
                {"@type": "ListItem", "position": 2, "name": "Облигации", "item": _BASE_URL + "/bond"},
                {"@type": "ListItem", "position": 3, "name": f"{s.name} ({secid})", "item": url},
            ],
        },
        {
            "@context": "https://schema.org",
            "@type": "FinancialProduct",
            "name": s.name,
            "alternateName": secid,
            "category": "Облигация",
            "url": url,
            "description": description,
        },
    ]
    if faq:
        jsonld.append({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}}
                for q, a in faq
            ],
        })

    body = f"""
<nav class="crumbs"><a href="/">Главная</a> / <a href="/bond">Облигации</a> / {e(secid)}</nav>
<h1>{e(s.name)} ({e(secid)}): цена, доходность и купоны</h1>
<p class="sub">{e(kind.capitalize())} · данные Московской биржи на {e(_fmt_date(today) or "")}</p>
<div class="kpis">{"".join(kpis)}</div>
{notes}
<h2>Обзор</h2>
<p>{e(description)}</p>
<h2>Параметры облигации</h2>
<table class="params">{params_html}</table>
{f'<h2>Вопросы и ответы</h2>{faq_html}' if faq_html else ''}
<div class="cta-box">
  <h2>Следите за {e(s.name)} в своём портфеле</h2>
  <p>Bond AI бесплатно отслеживает цены, купоны и доходность ваших облигаций: купонный календарь,
  Telegram-уведомления о выплатах, импорт портфеля из Т-Банка.</p>
  <a class="btn btn-primary" href="/app?auth=register">Создать портфель бесплатно</a>
</div>
{related_html}
"""
    return _page_shell(title, meta_desc, url, jsonld, body, og_type="article")


@router.api_route("/bond/{secid}", response_class=HTMLResponse, methods=["GET", "HEAD"])
async def bond_page(secid: str) -> HTMLResponse:
    """Public SEO page for a single bond (no auth)."""
    secid = secid.upper().strip()
    if not _SECID_RE.match(secid):
        return _render_404()
    now = time.time()

    cached = _page_cache.get(secid)
    if cached and now - cached[1] < _PAGE_TTL:
        return HTMLResponse(cached[0], headers=_PUBLIC_CACHE)
    nf = _notfound_cache.get(secid)
    if nf and now - nf < _NOTFOUND_TTL:
        return _render_404()

    try:
        snapshot = await moex_service.get_bond_snapshot(secid)
    except MOEXError:
        if len(_notfound_cache) > 2000:
            _notfound_cache.clear()
        _notfound_cache[secid] = now
        return _render_404()
    except Exception as exc:
        logger.warning("bond_page: snapshot failed for %s: %s", secid, exc)
        return _render_404()

    all_bonds = await _catalog_bonds()
    me = next((b for b in all_bonds if b["ticker"] == secid), None)
    board = me["board"] if me else None
    my_yield = (me or {}).get("market_yield") or snapshot.market_yield or 0
    pool = [b for b in all_bonds if b["ticker"] != secid and (board is None or b["board"] == board)]
    related = sorted(pool, key=lambda b: abs((b.get("market_yield") or 0) - my_yield))[:8]

    html = _render_bond_page(snapshot, board, related)
    _cache_put(secid, html)
    return HTMLResponse(html, headers=_PUBLIC_CACHE)


def _render_404() -> HTMLResponse:
    body = """
<nav class="crumbs"><a href="/">Главная</a> / <a href="/bond">Облигации</a></nav>
<h1>Облигация не найдена</h1>
<p>Такой бумаги нет на Московской бирже, либо она больше не торгуется.</p>
<p><a href="/bond">Каталог облигаций →</a></p>
"""
    html = _page_shell("Облигация не найдена | Bond AI",
                       "Облигация не найдена или не торгуется на MOEX.",
                       _BASE_URL + "/bond", [], body)
    return HTMLResponse(html, status_code=404, headers={"Cache-Control": "no-store"})


# Shared centred-card layout for error pages, rendered through the common shell
# so 404 / share-error match the rest of the site (topbar, footer, theme, mesh).
_ERROR_CSS = """<style>
.err-card{max-width:440px;margin:48px auto;text-align:center;
background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius-lg);
padding:40px 32px}
.err-icon{font-size:40px;line-height:1;margin-bottom:14px}
.err-card h1{font-size:22px;margin:0 0 10px}
.err-card p{color:var(--text);margin:0 0 22px;line-height:1.6}
</style>"""


def error_page_html(icon: str, heading: str, message: str,
                    cta_label: str = "← На главную", cta_href: str = "/",
                    title: str = "Bond AI") -> str:
    """Full HTML for a centred error page (404, broken share link, …)."""
    body = (
        _ERROR_CSS
        + '<div class="err-card"><div class="err-icon">' + icon + "</div>"
        + "<h1>" + heading + "</h1><p>" + message + "</p>"
        + '<a href="' + cta_href + '" class="btn btn-primary">' + cta_label + "</a></div>"
    )
    return _page_shell(title, message, _BASE_URL + "/", [], body)


# ── Catalog ──────────────────────────────────────────────────────────────────

_CATALOG_CSS = """<style>
.cat-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px}
.chips{display:flex;gap:6px}
.chip{font-size:13px;font-weight:600;color:var(--text);background:var(--panel);
border:1px solid var(--line-3);border-radius:999px;padding:6px 14px;cursor:pointer;
transition:all .15s;font-family:inherit}
.chip:hover{border-color:var(--line-strong);color:var(--head)}
.chip.active{background:linear-gradient(135deg,#2563eb,#4f46e5);border-color:transparent;color:#fff}
.cat-search{flex:1;min-width:180px;background:var(--input-bg);border:1px solid var(--line-3);
border-radius:var(--radius);color:var(--text-strong);padding:8px 12px;font-size:13px;font-family:inherit}
.cat-search:focus{outline:none;border-color:var(--blue-500)}
.yield-box{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}
.yield-box input{width:64px;background:var(--input-bg);border:1px solid var(--line-3);
border-radius:var(--radius);color:var(--text-strong);padding:8px 8px;font-size:13px;font-family:inherit}
.yield-box input:focus{outline:none;border-color:var(--blue-500)}
.cat-count{font-size:12px;color:var(--faint);margin:0 0 8px}
/* The 12-column table needs more than the 920px article width: widen the page
   shell for the catalog only, so nothing is cut off on a normal desktop. */
body.catalog main.wrap{max-width:1320px}
.table-scroll{overflow-x:auto;border:1px solid var(--line-2);border-radius:var(--radius-lg)}
.cat-table{min-width:1080px;font-size:13px;table-layout:auto}
.cat-table thead th{position:sticky;top:0;background:var(--panel-2);cursor:pointer;user-select:none;
white-space:nowrap;padding:10px 8px;line-height:1.25}
.cat-table thead th:hover{color:var(--text-soft)}
.cat-table thead th .dir{color:var(--link);font-size:10px;margin-left:3px}
.cat-table thead th .th-sub{display:block;font-weight:400;font-size:10px;color:var(--faint)}
.cat-table tbody tr:hover{background:var(--hover-bg)}
.cat-table td{white-space:nowrap;padding:8px}
/* Name is the only elastic column — it absorbs the leftover width and truncates
   instead of pushing the rating and date columns out of view. */
.cat-table td.nm{max-width:190px;overflow:hidden;text-overflow:ellipsis}
.cat-table td.idx{color:var(--faint);font-size:12px}
.cat-table td.y{color:var(--green);font-weight:600}
.rating-pill{display:inline-block;font-size:11px;font-weight:700;padding:1px 8px;border-radius:999px;
background:var(--pill-bg);color:var(--pill-tx)}
.cat-table td.risk{color:var(--red)}
.strat-label{font-size:12px;color:var(--muted)}
.chip.mini{font-size:11px;padding:4px 10px}
.ymap-legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--muted)}
.ymap-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
.ymap-legend .lg-0{background:#60a5fa}.ymap-legend .lg-1{background:#4ade80}
.ymap-legend .lg-2{background:#fbbf24}.ymap-legend .lg-3{background:#94a3b8}
html[data-theme=light] .ymap-legend .lg-0{background:#2563eb}
html[data-theme=light] .ymap-legend .lg-1{background:#15803d}
html[data-theme=light] .ymap-legend .lg-2{background:#d97706}
html[data-theme=light] .ymap-legend .lg-3{background:#94a3b8}
.ymap-box{background:var(--panel);border:1px solid var(--line-2);
border-radius:var(--radius-lg);padding:18px 18px 12px;margin:0 0 26px}
.ymap-head{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:4px}
.ymap-box h2{font-size:18px;margin:0;padding:0;border:none;flex:1}
.ymap-sub{font-size:13px;color:var(--muted);margin:0 0 12px}
.ymap-wrap{position:relative}
#ymap{display:block;width:100%;height:420px;cursor:crosshair;touch-action:pan-y}
.ymap-zoom{position:absolute;top:8px;right:8px;display:flex;gap:5px;z-index:4}
.ymap-zoom button{width:28px;height:28px;display:flex;align-items:center;justify-content:center;
background:var(--panel-2);border:1px solid var(--line-3);border-radius:var(--radius-sm);
color:var(--text-soft);font-size:15px;font-weight:700;cursor:pointer;font-family:inherit;
transition:all .15s;padding:0;line-height:1}
.ymap-zoom button:hover{border-color:var(--line-strong);color:var(--head)}
.ymap-zoom button#ymap-reset{width:auto;padding:0 9px;font-size:11px;font-weight:600;display:none}
#ymap-tip{position:absolute;display:none;pointer-events:none;z-index:5;
background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--radius);
padding:8px 11px;font-size:12px;color:var(--text-soft);box-shadow:0 8px 24px rgba(0,0,0,.35);
white-space:nowrap}
#ymap-tip b{color:var(--head);display:block;margin-bottom:2px}
#ymap-tip .ty{color:var(--green);font-weight:600}
.ymap-note{font-size:12px;color:var(--faint);margin-top:8px}
/* 12 columns never fit a narrow screen: keep the horizontal scroll but make it
   obvious it exists. Freezing the name column must cover the whole range where
   the table scrolls, not just phones — otherwise a scrolled row on a tablet
   loses the only cell that identifies it. The table needs a 1080px track, so the
   scroll disappears at a 1130px viewport: that measured value is the breakpoint. */
.scroll-hint{display:none;font-size:12px;color:var(--faint);margin:0 0 6px}
@media(max-width:1129px){.scroll-hint{display:block}
.cat-table thead th:nth-child(2),.cat-table td.nm{position:sticky;left:0;z-index:2;
background:var(--panel);box-shadow:1px 0 0 var(--line-3)}
.cat-table thead th:nth-child(2){z-index:3;background:var(--panel-2)}
.cat-table tbody tr:hover td.nm{background:var(--hover-bg)}}
@media(max-width:680px){#ymap{height:300px}
.cat-table{font-size:12px}
.cat-table thead th,.cat-table td{padding:7px 6px}
.cat-table td.nm{max-width:132px}
.cat-table thead th:first-child,.cat-table td.idx{display:none}}
</style>"""

_MAP_JS = """<script>
(function(){
  var data=JSON.parse(document.getElementById('map-data').textContent);
  var canvas=document.getElementById('ymap');
  if(!canvas)return;
  var ctx=canvas.getContext('2d');
  var tip=document.getElementById('ymap-tip');
  var note=document.getElementById('ymap-note');
  var resetBtn=document.getElementById('ymap-reset');
  var group=0,xmode=0,pts=[],hover=-1;
  var base=null,xdom=null,ydom=null;
  var PADL=46,PADR=14,PADT=14,PADB=32;

  function pal(){
    var l=document.documentElement.getAttribute('data-theme')==='light';
    return l?{grid:'rgba(15,23,42,.08)',grid2:'rgba(15,23,42,.05)',tick:'#64748b',axis:'#475569',
      hover:'#0f172a',rb:['#2563eb','#15803d','#d97706','#94a3b8']}
    :{grid:'rgba(148,163,184,.08)',grid2:'rgba(148,163,184,.06)',tick:'#64748b',axis:'#94a3b8',
      hover:'#fff',rb:['#60a5fa','#4ade80','#fbbf24','#94a3b8']};
  }
  function quantile(sorted,q){
    if(!sorted.length)return 0;
    var pos=(sorted.length-1)*q,lo=Math.floor(pos),hi=Math.ceil(pos);
    return sorted[lo]+(sorted[hi]-sorted[lo])*(pos-lo);
  }
  function niceStep(range,target){
    var raw=range/target,mag=Math.pow(10,Math.floor(Math.log(raw)/Math.LN10));
    var c=[1,2,2.5,5,10],best=c[0]*mag;
    for(var i=0;i<c.length;i++){if(c[i]*mag>=raw){best=c[i]*mag;break;}}
    return best;
  }
  function xv(p){return xmode?p.yr:p.d;}

  function resetDomain(){
    if(!pts.length){base=null;xdom=null;ydom=null;return;}
    var xs=pts.map(xv),yv=pts.map(function(p){return p.y;});
    var xmax=Math.max(1,Math.ceil(Math.max.apply(null,xs)+0.3));
    var ymin=Math.min.apply(null,yv),ymax=Math.max.apply(null,yv);
    var ypad=Math.max(0.4,(ymax-ymin)*0.07);
    base={x0:0,x1:xmax,y0:Math.max(0,ymin-ypad),y1:ymax+ypad};
    xdom=[base.x0,base.x1];ydom=[base.y0,base.y1];
  }
  function zoomed(){
    return base&&(Math.abs(xdom[0]-base.x0)>1e-9||Math.abs(xdom[1]-base.x1)>1e-9
      ||Math.abs(ydom[0]-base.y0)>1e-9||Math.abs(ydom[1]-base.y1)>1e-9);
  }
  function syncReset(){resetBtn.style.display=zoomed()?'flex':'none';}

  function build(){
    var rows=data.filter(function(r){return r[4]===group;});
    var ys=rows.map(function(r){return r[2];}).sort(function(a,b){return a-b;});
    var hi=ys.length?Math.min(quantile(ys,0.99)*1.25,60):60;
    var hidden=0;
    pts=[];
    rows.forEach(function(r){
      if(r[2]>hi){hidden++;return;}
      pts.push({t:r[0],n:r[1],y:r[2],d:r[3],yr:r[5],rb:r[6]});
    });
    note.textContent=pts.length+' бумаг · наведите на точку для деталей, клик — страница облигации'
      +' · масштаб: колесо мыши или кнопки + / −, перетаскивание — сдвиг'
      +(hidden?' · скрыто аномалий: '+hidden:'');
    hover=-1;resetDomain();syncReset();draw();
  }

  function draw(){
    var w=canvas.clientWidth,h=canvas.clientHeight,dpr=window.devicePixelRatio||1;
    canvas.width=w*dpr;canvas.height=h*dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,w,h);
    var P=pal();
    if(!pts.length){ctx.fillStyle=P.tick;ctx.font='13px Inter,sans-serif';
      ctx.fillText('Нет данных',w/2-30,h/2);return;}
    var pw=w-PADL-PADR,ph=h-PADT-PADB;
    var x0=xdom[0],x1=xdom[1],y0=ydom[0],y1=ydom[1];
    function X(v){return PADL+(v-x0)/(x1-x0)*pw;}
    function Y(v){return PADT+(1-(v-y0)/(y1-y0))*ph;}
    ctx.font='11px Inter,sans-serif';
    var ystep=niceStep(y1-y0,6);
    for(var y=Math.ceil(y0/ystep)*ystep;y<=y1+1e-9;y+=ystep){
      var py=Y(y);
      ctx.strokeStyle=P.grid;ctx.beginPath();
      ctx.moveTo(PADL,py);ctx.lineTo(w-PADR,py);ctx.stroke();
      ctx.fillStyle=P.tick;ctx.textAlign='right';
      ctx.fillText((Math.round(y*10)/10)+'%',PADL-7,py+4);
    }
    var xstep=niceStep(x1-x0,8);
    for(var x=Math.ceil(x0/xstep)*xstep;x<=x1+1e-9;x+=xstep){
      var px=X(x);
      ctx.strokeStyle=P.grid2;ctx.beginPath();
      ctx.moveTo(px,PADT);ctx.lineTo(px,h-PADB);ctx.stroke();
      ctx.fillStyle=P.tick;ctx.textAlign='center';
      ctx.fillText(Math.round(x*100)/100,px,h-PADB+16);
    }
    ctx.textAlign='left';ctx.fillStyle=P.axis;
    ctx.fillText(xmode?'Срок, лет':'Дюрация, лет',w-PADR-78,h-6);
    ctx.save();ctx.translate(11,PADT+86);ctx.rotate(-Math.PI/2);
    ctx.fillText('Доходность, %',0,0);ctx.restore();
    ctx.save();
    ctx.beginPath();ctx.rect(PADL,PADT,pw,ph);ctx.clip();
    var few=pts.length<=120;
    var r=few?4:3;
    pts.forEach(function(p,i){
      p.px=X(xv(p));p.py=Y(p.y);
      ctx.beginPath();ctx.arc(p.px,p.py,i===hover?r+2:r,0,Math.PI*2);
      ctx.fillStyle=i===hover?P.hover:P.rb[p.rb]||P.rb[3];
      ctx.globalAlpha=few?0.95:0.65;ctx.fill();ctx.globalAlpha=1;
    });
    ctx.restore();
  }

  function zoom(f,cx,cy){
    // f<1 zoom in, f>1 zoom out; (cx,cy) = anchor in data coords
    var nx0=cx-(cx-xdom[0])*f,nx1=cx+(xdom[1]-cx)*f;
    var ny0=cy-(cy-ydom[0])*f,ny1=cy+(ydom[1]-cy)*f;
    if(nx1-nx0<0.1||ny1-ny0<0.2)return;
    if(nx1-nx0>(base.x1-base.x0)*3)return;
    xdom=[nx0,nx1];ydom=[ny0,ny1];
    syncReset();draw();
  }
  function dataXY(mx,my){
    var w=canvas.clientWidth,h=canvas.clientHeight;
    var pw=w-PADL-PADR,ph=h-PADT-PADB;
    return [xdom[0]+(mx-PADL)/pw*(xdom[1]-xdom[0]),
            ydom[0]+(1-(my-PADT)/ph)*(ydom[1]-ydom[0])];
  }
  canvas.addEventListener('wheel',function(ev){
    ev.preventDefault();
    var rect=canvas.getBoundingClientRect();
    var c=dataXY(ev.clientX-rect.left,ev.clientY-rect.top);
    zoom(ev.deltaY<0?0.8:1.25,c[0],c[1]);
  },{passive:false});
  document.getElementById('ymap-zin').addEventListener('click',function(){
    zoom(0.7,(xdom[0]+xdom[1])/2,(ydom[0]+ydom[1])/2);
  });
  document.getElementById('ymap-zout').addEventListener('click',function(){
    zoom(1.4,(xdom[0]+xdom[1])/2,(ydom[0]+ydom[1])/2);
  });
  resetBtn.addEventListener('click',function(){
    resetDomain();syncReset();draw();
  });
  canvas.addEventListener('dblclick',function(){
    resetDomain();syncReset();draw();
  });

  var drag=null;
  canvas.addEventListener('mousedown',function(ev){
    var rect=canvas.getBoundingClientRect();
    drag={mx:ev.clientX-rect.left,my:ev.clientY-rect.top,
          x0:xdom[0],x1:xdom[1],y0:ydom[0],y1:ydom[1],moved:false};
  });
  function nearest(mx,my){
    var best=-1,bd=144;
    pts.forEach(function(p,i){
      var dx=p.px-mx,dy=p.py-my,d2=dx*dx+dy*dy;
      if(d2<bd){bd=d2;best=i;}
    });
    return best;
  }
  canvas.addEventListener('mousemove',function(ev){
    var rect=canvas.getBoundingClientRect();
    var mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    if(drag){
      var w=canvas.clientWidth,h=canvas.clientHeight;
      var pw=w-PADL-PADR,ph=h-PADT-PADB;
      var dx=(mx-drag.mx)/pw*(drag.x1-drag.x0);
      var dy=(my-drag.my)/ph*(drag.y1-drag.y0);
      if(Math.abs(mx-drag.mx)+Math.abs(my-drag.my)>4)drag.moved=true;
      if(drag.moved){
        xdom=[drag.x0-dx,drag.x1-dx];ydom=[drag.y0+dy,drag.y1+dy];
        tip.style.display='none';hover=-1;
        syncReset();draw();
        canvas.style.cursor='grabbing';
        return;
      }
    }
    var i=nearest(mx,my);
    if(i!==hover){hover=i;draw();}
    if(i>=0){
      var p=pts[i];
      tip.innerHTML='<b></b><span class="ty"></span> · дюрация <span class="td"></span> лет · срок <span class="tm"></span> лет';
      tip.querySelector('b').textContent=p.n;
      tip.querySelector('.ty').textContent=p.y.toFixed(2).replace('.',',')+'%';
      tip.querySelector('.td').textContent=p.d.toFixed(1).replace('.',',');
      tip.querySelector('.tm').textContent=p.yr.toFixed(1).replace('.',',');
      tip.style.display='block';
      var tx=p.px+14,ty=p.py-14;
      if(tx+tip.offsetWidth>canvas.clientWidth-4)tx=p.px-tip.offsetWidth-14;
      if(ty<0)ty=p.py+14;
      tip.style.left=tx+'px';tip.style.top=ty+'px';
      canvas.style.cursor='pointer';
    }else{tip.style.display='none';canvas.style.cursor='crosshair';}
  });
  var suppressClick=false;
  window.addEventListener('mouseup',function(){
    if(drag&&drag.moved)canvas.style.cursor='crosshair';
    var moved=drag&&drag.moved;
    drag=null;
    if(moved)suppressClick=true;
  });
  canvas.addEventListener('click',function(){
    if(suppressClick){suppressClick=false;return;}
    if(hover>=0)location.href='/bond/'+encodeURIComponent(pts[hover].t);
  });
  canvas.addEventListener('mouseleave',function(){
    hover=-1;tip.style.display='none';draw();
  });
  document.querySelectorAll('#map-chips .chip').forEach(function(ch){
    ch.addEventListener('click',function(){
      document.querySelectorAll('#map-chips .chip').forEach(function(c){c.classList.remove('active');});
      ch.classList.add('active');group=parseInt(ch.dataset.g,10);build();
    });
  });
  document.querySelectorAll('#x-chips .chip').forEach(function(ch){
    ch.addEventListener('click',function(){
      document.querySelectorAll('#x-chips .chip').forEach(function(c){c.classList.remove('active');});
      ch.classList.add('active');xmode=parseInt(ch.dataset.x,10);hover=-1;
      resetDomain();syncReset();draw();
    });
  });
  var rt;
  window.addEventListener('resize',function(){
    clearTimeout(rt);rt=setTimeout(draw,120);
  });
  window.addEventListener('bondai-theme',draw);
  build();
})();
</script>"""

_CATALOG_JS = """<script>
(function(){
  var tbody=document.getElementById('cat-body');
  var rows=Array.prototype.slice.call(tbody.rows);
  var search=document.getElementById('cat-search');
  var ymin=document.getElementById('y-min'), ymax=document.getElementById('y-max');
  var count=document.getElementById('cat-count');
  var board='all', sortCol=-1, sortDir=1, strat='';

  // High-yield threshold: OFZ median yield + 4pp (computed from the yield-map data)
  var hyMin=18;
  try{
    var mp=JSON.parse(document.getElementById('map-data').textContent);
    var ofzY=mp.filter(function(r){return r[4]===0;}).map(function(r){return r[2];}).sort(function(a,b){return a-b;});
    if(ofzY.length) hyMin=ofzY[Math.floor(ofzY.length/2)]+4;
  }catch(e){}

  function stratOk(tr){
    if(!strat) return true;
    var rb=parseInt(tr.dataset.rb,10), yr=parseFloat(tr.dataset.years||'');
    var y=parseFloat(tr.dataset.yield||'');
    if(strat==='deposit') return rb===0&&yr>=1&&yr<=3;
    if(strat==='balanced') return rb<=1&&yr>=1&&yr<=5;
    if(strat==='hy') return rb===2&&y>=hyMin;
    return true;
  }
  function apply(){
    var q=(search.value||'').toLowerCase().trim();
    var lo=parseFloat(ymin.value), hi=parseFloat(ymax.value);
    var shown=0;
    rows.forEach(function(tr){
      var ok=(board==='all'||tr.dataset.board===board);
      if(ok) ok=stratOk(tr);
      if(ok&&q) ok=tr.dataset.search.indexOf(q)!==-1;
      if(ok&&!isNaN(lo)) ok=parseFloat(tr.dataset.yield||'')>=lo;
      if(ok&&!isNaN(hi)) ok=parseFloat(tr.dataset.yield||'')<=hi;
      tr.style.display=ok?'':'none';
      if(ok){shown++;tr.cells[0].textContent=shown;}
    });
    count.textContent='Показано '+shown+' из '+rows.length;
  }
  document.querySelectorAll('#strat-chips .chip').forEach(function(ch){
    ch.addEventListener('click',function(){
      var on=ch.classList.contains('active');
      document.querySelectorAll('#strat-chips .chip').forEach(function(c){c.classList.remove('active');});
      if(on){strat='';}else{ch.classList.add('active');strat=ch.dataset.strat;}
      apply();
    });
  });
  document.querySelectorAll('#cat-chips .chip').forEach(function(ch){
    ch.addEventListener('click',function(){
      document.querySelectorAll('#cat-chips .chip').forEach(function(c){c.classList.remove('active');});
      ch.classList.add('active');board=ch.dataset.board;apply();
    });
  });
  search.addEventListener('input',apply);
  ymin.addEventListener('input',apply);ymax.addEventListener('input',apply);

  document.querySelectorAll('#cat-table thead th[data-col]').forEach(function(th){
    th.addEventListener('click',function(){
      var col=parseInt(th.dataset.col,10);
      sortDir=(sortCol===col)?-sortDir:1;sortCol=col;
      document.querySelectorAll('#cat-table thead .dir').forEach(function(d){d.textContent='';});
      th.querySelector('.dir').textContent=sortDir===1?'\\u25b2':'\\u25bc';
      rows.sort(function(a,b){
        var av=a.cells[col].dataset.v, bv=b.cells[col].dataset.v;
        var an=parseFloat(av), bn=parseFloat(bv);
        var aEmpty=(av===''||av===undefined), bEmpty=(bv===''||bv===undefined);
        if(aEmpty&&bEmpty) return 0;
        if(aEmpty) return 1;
        if(bEmpty) return -1;
        if(!isNaN(an)&&!isNaN(bn)) return (an-bn)*sortDir;
        return String(av).localeCompare(String(bv))*sortDir;
      });
      rows.forEach(function(r){tbody.appendChild(r);});
      apply();
    });
  });
  apply();
})();
</script>"""


@router.api_route("/bond", response_class=HTMLResponse, methods=["GET", "HEAD"])
async def bonds_catalog() -> HTMLResponse:
    """Public catalog of all traded MOEX bonds — crawl entry point for /bond/{secid}.

    Smart-lab-style structure: type tabs (chips), search, yield range filter and
    client-side sortable columns over a single server-rendered table.
    """
    global _catalog_cache
    now = time.time()
    if _catalog_cache and now - _catalog_cache[1] < _CATALOG_TTL:
        return HTMLResponse(_catalog_cache[0], headers=_PUBLIC_CACHE)

    bonds = await _catalog_bonds()
    today = date.today()
    ofz = sorted((b for b in bonds if b["board"] == "TQOB"), key=lambda b: b.get("maturity") or "")
    corp = sorted((b for b in bonds if b["board"] != "TQOB"), key=lambda b: b["name"])
    ordered = ofz + corp

    def _dmy(iso: str | None) -> tuple[str, str]:
        """(display dd.mm.yyyy, sort key iso) or em-dash."""
        if not iso:
            return "—", ""
        try:
            d = date.fromisoformat(iso)
            return f"{d.day:02d}.{d.month:02d}.{d.year}", iso
        except ValueError:
            return "—", ""

    rows_html: list[str] = []
    # Yield map points: [secid, name, ytm, duration, group, years, rating_bucket]
    # group: 0 = ОФЗ, 1 = corporate RUB, 2 = FX-denominated (замещающие и юаневые)
    map_points: list[list] = []
    for i, b in enumerate(ordered, 1):
        mat_disp, mat_key = _dmy(b.get("maturity"))
        off_disp, off_key = _dmy(b.get("offer_date"))
        years_disp, years_key = "—", ""
        if mat_key:
            yrs = (date.fromisoformat(mat_key) - today).days / 365
            years_disp, years_key = f"{yrs:.1f}".replace(".", ","), f"{yrs:.2f}"
        my = b.get("market_yield")
        rating = b.get("rating")
        rb = _rating_bucket(rating, b["board"])
        rating_html = f'<span class="rating-pill">{e(rating)}</span>' if rating else "—"

        # Duration to the nearest event (offer if sooner than maturity), like the yield map
        horizon_yrs = None
        for iso_key in (off_key, mat_key):
            if iso_key:
                d_evt = date.fromisoformat(iso_key)
                if d_evt > today:
                    h = (d_evt - today).days / 365
                    if horizon_yrs is None or h < horizon_yrs:
                        horizon_yrs = h
        dur = None
        if my and 0 < my <= 100 and horizon_yrs:
            dur = _macaulay_duration(b.get("coupon_percent"), b.get("coupon_frequency"),
                                     horizon_yrs, my)
        dur_disp, dur_key = "—", ""
        risk_disp, risk_key = "—", ""
        if dur is not None:
            dur_disp, dur_key = f"{dur:.1f}".replace(".", ","), f"{dur:.2f}"
            # price change at +2pp rates ≈ −modified duration × 2
            risk = -dur / (1 + my / 100.0) * 2
            risk_disp, risk_key = f"{risk:.1f}".replace(".", ","), f"{risk:.2f}"
            # FX first: CNY sovereign bonds live on TQOB but belong on the FX map
            if (b.get("face_unit") or "SUR") not in ("SUR", "RUB"):
                g = 2
            elif b["board"] == "TQOB":
                g = 0
            else:
                g = 1
            map_points.append([b["ticker"], b["name"], round(float(my), 2),
                               round(dur, 2), g, round(horizon_yrs, 2), rb])

        rows_html.append(
            f'<tr data-board="{e(b["board"])}" data-yield="{my if my is not None else ""}"'
            f' data-years="{years_key}" data-rb="{rb}"'
            f' data-search="{e((b["name"] + " " + b["ticker"]).lower())}">'
            f'<td class="idx num" data-v="{i}">{i}</td>'
            f'<td class="nm" data-v="{e(b["name"])}" title="{e(b["name"])}">'
            f'<a href="/bond/{e(b["ticker"])}">{e(b["name"])}</a></td>'
            f'<td class="num" data-v="{years_key}">{years_disp}</td>'
            f'<td class="num" data-v="{dur_key}">{dur_disp}</td>'
            f'<td class="num y" data-v="{my if my is not None else ""}">{_fmt_money(my) or "—"}</td>'
            f'<td class="num" data-v="{b.get("coupon_percent") or ""}">{_fmt_money(b.get("coupon_percent")) or "—"}</td>'
            f'<td class="num" data-v="{b.get("coupon_frequency") or ""}">{b.get("coupon_frequency") or "—"}</td>'
            f'<td class="num" data-v="{b["price"]}">{_fmt_money(b["price"])}</td>'
            f'<td class="num risk" data-v="{risk_key}">{risk_disp}</td>'
            f'<td data-v="{e(rating or "")}">{rating_html}</td>'
            f'<td class="num" data-v="{mat_key}">{mat_disp}</td>'
            f'<td class="num" data-v="{off_key}">{off_disp}</td>'
            "</tr>"
        )
    map_json = json.dumps(map_points, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    today_str = _fmt_date(today)
    body = f"""{_CATALOG_CSS}
<script>document.body.classList.add('catalog');</script>
<nav class="crumbs"><a href="/">Главная</a> / Облигации</nav>
<h1>Облигации Московской биржи: цены, доходность и карта доходности</h1>
<p class="sub">{len(bonds)} торгуемых выпусков · ОФЗ, корпоративные и валютные облигации · данные MOEX на {e(today_str or "")}</p>
<section class="ymap-box">
  <div class="ymap-head">
    <h2>Карта доходности облигаций</h2>
    <div class="chips" id="map-chips">
      <button class="chip active" data-g="0">ОФЗ</button>
      <button class="chip" data-g="1">Корпоративные</button>
      <button class="chip" data-g="2">Валютные</button>
    </div>
    <div class="chips" id="x-chips">
      <button class="chip mini active" data-x="0">Дюрация</button>
      <button class="chip mini" data-x="1">Срок</button>
    </div>
  </div>
  <p class="ymap-sub">Рыночная доходность к погашению (YTM) по дюрации или сроку: каждая точка — выпуск
  облигации, цвет — кредитный рейтинг. Карта доходности ОФЗ показывает кривую госбумаг, корпоративные
  торгуются с премией к ней, валютные (замещающие и юаневые) — в своей шкале доходности.</p>
  <div class="ymap-wrap">
    <canvas id="ymap"></canvas>
    <div class="ymap-zoom">
      <button id="ymap-zin" title="Приблизить">+</button>
      <button id="ymap-zout" title="Отдалить">&minus;</button>
      <button id="ymap-reset" title="Вернуть исходный масштаб">Сброс</button>
    </div>
    <div id="ymap-tip"></div>
  </div>
  <div class="ymap-legend">
    <span><i class="lg-0"></i>AAA–AA и ОФЗ</span>
    <span><i class="lg-1"></i>A–BBB</span>
    <span><i class="lg-2"></i>BB и ниже</span>
    <span><i class="lg-3"></i>без рейтинга</span>
  </div>
  <div class="ymap-note" id="ymap-note"></div>
</section>
<script id="map-data" type="application/json">{map_json}</script>
<div class="cat-toolbar" style="margin-bottom:8px">
  <span class="strat-label">Стратегии:</span>
  <div class="chips" id="strat-chips">
    <button class="chip" data-strat="deposit" title="Надёжные бумаги (ОФЗ и рейтинг AAA–AA) со сроком 1–3 года">Альтернатива депозиту</button>
    <button class="chip" data-strat="balanced" title="Рейтинг BBB и выше, срок 1–5 лет">Сбалансированная</button>
    <button class="chip" data-strat="hy" title="Высокая доходность, рейтинг BB и ниже — повышенный риск">High Yield (ВДО)</button>
  </div>
</div>
<div class="cat-toolbar">
  <div class="chips" id="cat-chips">
    <button class="chip active" data-board="all">Все</button>
    <button class="chip" data-board="TQOB">ОФЗ</button>
    <button class="chip" data-board="TQCB">Корпоративные</button>
  </div>
  <input class="cat-search" id="cat-search" type="search" placeholder="Поиск по названию или тикеру…">
  <div class="yield-box">Доходность, %:
    <input id="y-min" type="number" placeholder="от" step="any">
    <input id="y-max" type="number" placeholder="до" step="any">
  </div>
</div>
<div class="cat-count" id="cat-count"></div>
<p class="scroll-hint">← Таблицу можно прокручивать по горизонтали: рейтинг, погашение и оферта — справа.</p>
<div class="table-scroll">
<table class="cat-table" id="cat-table">
<thead><tr>
  <th>№</th>
  <th data-col="1">Название<span class="dir"></span></th>
  <th data-col="2" title="Лет до погашения">Лет<span class="dir"></span></th>
  <th data-col="3" title="Дюрация Маколея к ближайшему событию (оферта или погашение)">Дюрация<span class="dir"></span></th>
  <th data-col="4" title="Доходность к погашению (YTM), % годовых">Доходн.&nbsp;%<span class="dir"></span></th>
  <th data-col="5">Купон&nbsp;%<span class="dir"></span></th>
  <th data-col="6" title="Число купонных выплат в год">Выплат<span class="dir"></span></th>
  <th data-col="7" title="Цена в % от номинала">Цена&nbsp;%<span class="dir"></span></th>
  <th data-col="8" title="Примерное изменение цены при росте ключевой ставки на 2 п.п. (рыночный риск)">При&nbsp;+2%<span class="dir"></span></th>
  <th data-col="9">Рейтинг<span class="dir"></span></th>
  <th data-col="10">Погашение<span class="dir"></span></th>
  <th data-col="11">Оферта<span class="dir"></span></th>
</tr></thead>
<tbody id="cat-body">{"".join(rows_html)}</tbody>
</table>
</div>
<p style="margin-top:18px">Каталог облигаций, торгующихся на Московской бирже: ОФЗ и корпоративные облигации
с текущей ценой, доходностью к погашению, купоном и датами погашения и оферты. Сортируйте таблицу кликом
по заголовку колонки, фильтруйте по типу и доходности. На странице каждой бумаги — купонный календарь,
НКД, рейтинг и FAQ. Незнакомые термины — в <a href="/uchebnik">учебнике по облигациям</a>,
посчитать самостоятельно — в <a href="/calc">калькуляторах НКД и YTM</a>.</p>
<p>Карта доходности облигаций выше строится по рыночной доходности (YTM) и расчётной дюрации каждого
выпуска: отдельно карта доходности ОФЗ, корпоративных и валютных облигаций (замещающие и юаневые выпуски).
Чем правее точка — тем дольше срок и выше чувствительность цены к ставке; чем выше — тем больше доходность.
Выпуски с аномальной доходностью к близкой оферте на карте не показываются.</p>
<div class="cta-box">
  <h2>Соберите портфель из этих облигаций</h2>
  <p>Bond AI бесплатно посчитает доходность, покажет купонный календарь и предупредит об офертах.</p>
  <a class="btn btn-primary" href="/app?auth=register">Начать бесплатно</a>
</div>
{_CATALOG_JS}
{_MAP_JS}
"""
    jsonld = [{
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Облигации Московской биржи: каталог и карта доходности",
        "url": _BASE_URL + "/bond",
        "description": "Каталог торгуемых облигаций MOEX с картой доходности: ОФЗ, корпоративные и валютные выпуски — цены, доходность к погашению по дюрации, купоны и даты погашения.",
        "inLanguage": "ru",
    }]
    html = _page_shell(
        "Облигации MOEX: каталог и карта доходности | Bond AI",
        f"Карта доходности облигаций (ОФЗ, корпоративные, валютные) и каталог из {len(bonds)} выпусков Московской биржи: цены, доходность к погашению (YTM) по дюрации, купоны, оферты и рейтинги.",
        _BASE_URL + "/bond", jsonld, body,
    )
    if bonds:
        _catalog_cache = (html, now)
    return HTMLResponse(html, headers=_PUBLIC_CACHE)


# ── Sitemap ──────────────────────────────────────────────────────────────────

async def all_public_urls() -> list[str]:
    """Every public, indexable URL — static pages + one per tradable bond.

    Used by the IndexNow submitter so search engines learn about all bond pages
    at once instead of discovering them one crawl at a time.
    """
    static = [
        f"{_BASE_URL}/", f"{_BASE_URL}/uchebnik", f"{_BASE_URL}/bond",
        f"{_BASE_URL}/calc", f"{_BASE_URL}/calc/nkd", f"{_BASE_URL}/calc/ytm",
        f"{_BASE_URL}/privacy", f"{_BASE_URL}/terms",
    ]
    bonds = await _catalog_bonds()
    return static + [f"{_BASE_URL}/bond/{b['ticker']}" for b in bonds]


@router.api_route("/sitemap-bonds.xml", methods=["GET", "HEAD"])
async def sitemap_bonds() -> Response:
    """Sitemap of all bond pages, rebuilt from the hourly bonds cache."""
    global _sitemap_cache
    now = time.time()
    if _sitemap_cache and now - _sitemap_cache[1] < _CATALOG_TTL:
        return Response(_sitemap_cache[0], media_type="application/xml",
                        headers=_PUBLIC_CACHE)

    bonds = await _catalog_bonds()
    urls = [f"{_BASE_URL}/bond"] + [f"{_BASE_URL}/bond/{b['ticker']}" for b in bonds]
    entries = "".join(
        f"  <url><loc>{e(u)}</loc><changefreq>daily</changefreq><priority>0.6</priority></url>\n"
        for u in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}"
        "</urlset>\n"
    )
    if bonds:
        _sitemap_cache = (xml, now)
    return Response(xml, media_type="application/xml", headers=_PUBLIC_CACHE)
