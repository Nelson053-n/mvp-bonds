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
_SECID_RE = re.compile(r"^[A-Z0-9-]{4,24}$")

_PAGE_TTL = 900        # rendered bond page cache
_CATALOG_TTL = 3600    # rendered catalog page cache
_NOTFOUND_TTL = 600    # negative cache: unknown secids (protects MOEX from crawler junk)
_PAGE_CACHE_MAX = 800

_page_cache: dict[str, tuple[str, float]] = {}
_notfound_cache: dict[str, float] = {}
_catalog_cache: tuple[str, float] | None = None
_sitemap_cache: tuple[str, float] | None = None

_PUBLIC_CACHE = {"Cache-Control": "public, max-age=900"}

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
:root{--blue-400:#60a5fa;--blue-500:#3b82f6;--blue-600:#2563eb;
--green-400:#4ade80;--red-400:#f87171;--yellow-400:#fbbf24;
--slate-400:#94a3b8;--slate-500:#64748b;--slate-600:#475569;
--slate-700:#334155;--slate-800:#1e293b;--slate-900:#0f172a;
--radius:8px;--radius-lg:12px}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;background:#020817;color:#e2e8f0;
line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--blue-400);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:920px;margin:0 auto;padding:0 20px}
header{border-bottom:1px solid var(--slate-800);padding:14px 0}
.nav{display:flex;align-items:center;gap:18px}
.logo{display:flex;align-items:center;gap:8px;font-weight:800;font-size:17px;color:#fff}
.logo-badge{width:26px;height:26px;border-radius:7px;background:var(--blue-600);color:#fff;
display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:700}
.nav-links{display:flex;gap:16px;margin-left:auto;align-items:center;font-size:14px}
.nav-links a{color:var(--slate-400)}
.btn{display:inline-block;background:var(--blue-600);color:#fff!important;padding:9px 18px;
border-radius:var(--radius);font-weight:600;font-size:14px}
.btn:hover{background:var(--blue-500);text-decoration:none}
.crumbs{font-size:13px;color:var(--slate-500);margin:22px 0 8px}
.crumbs a{color:var(--slate-400)}
h1{font-size:26px;font-weight:800;color:#fff;line-height:1.3;margin:4px 0 6px}
.sub{font-size:14px;color:var(--slate-400);margin-bottom:22px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:26px}
.kpi{background:var(--slate-900);border:1px solid var(--slate-800);border-radius:var(--radius-lg);padding:14px 16px}
.kpi-label{font-size:12px;color:var(--slate-500);margin-bottom:4px}
.kpi-value{font-size:19px;font-weight:700;color:#fff;white-space:nowrap}
.kpi-value.green{color:var(--green-400)}.kpi-value.red{color:var(--red-400)}
.kpi-sub{font-size:12px;color:var(--slate-500);margin-top:2px}
h2{font-size:19px;font-weight:700;color:#fff;margin:30px 0 12px}
p{margin-bottom:12px;color:#cbd5e1;font-size:15px}
table.params{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:8px}
table.params td{padding:9px 12px;border-bottom:1px solid var(--slate-800)}
table.params td:first-child{color:var(--slate-400);width:46%}
table.params td:last-child{color:#e2e8f0;font-weight:500}
.note{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);
border-radius:var(--radius);padding:12px 16px;font-size:14px;color:#fcd34d;margin:14px 0}
.faq-item{margin-bottom:14px}
.faq-item h3{font-size:15px;font-weight:600;color:#fff;margin-bottom:4px}
.faq-item p{font-size:14px;color:var(--slate-400);margin:0}
.related{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.rel-card{background:var(--slate-900);border:1px solid var(--slate-800);border-radius:var(--radius);
padding:12px 14px;display:block}
.rel-card:hover{border-color:var(--slate-600);text-decoration:none}
.rel-name{font-size:13px;font-weight:600;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rel-meta{font-size:12px;color:var(--slate-500);margin-top:3px}
.rel-meta b{color:var(--green-400);font-weight:600}
.cta-box{background:linear-gradient(135deg,rgba(37,99,235,.15),rgba(37,99,235,.05));
border:1px solid rgba(59,130,246,.35);border-radius:var(--radius-lg);padding:24px;margin:34px 0;text-align:center}
.cta-box h2{margin:0 0 8px}
.cta-box p{font-size:14px;color:var(--slate-400);margin-bottom:16px}
footer{border-top:1px solid var(--slate-800);margin-top:40px;padding:22px 0 30px;
font-size:12px;color:var(--slate-600)}
footer p{font-size:12px;color:var(--slate-600);margin-bottom:6px}
.cat-table{width:100%;border-collapse:collapse;font-size:14px}
.cat-table th{text-align:left;padding:8px 10px;color:var(--slate-500);font-size:12px;
border-bottom:1px solid var(--slate-700)}
.cat-table td{padding:8px 10px;border-bottom:1px solid var(--slate-800)}
.cat-table td.num{text-align:right;white-space:nowrap}
@media(max-width:640px){h1{font-size:21px}.nav-links a.ghost{display:none}}
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
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:locale" content="ru_RU">
<meta property="og:site_name" content="Bond AI">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%232563eb'/><text x='16' y='23' font-family='Inter,Arial,sans-serif' font-size='20' font-weight='700' fill='white' text-anchor='middle'>B</text></svg>"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
{jsonld}
<style>{_CSS}</style>
</head>
<body>
<header><div class="wrap nav">
  <a class="logo" href="/"><span class="logo-badge">B</span>Bond AI</a>
  <nav class="nav-links">
    <a class="ghost" href="/bond">Облигации</a>
    <a class="ghost" href="/uchebnik">Учебник</a>
    <a href="/app?auth=login">Войти</a>
    <a class="btn" href="/app?auth=register">Начать бесплатно</a>
  </nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
<p>Данные — Московская биржа (MOEX ISS), обновляются в течение торгового дня. Информация носит справочный характер и не является индивидуальной инвестиционной рекомендацией.</p>
<p>© Bond AI · <a href="/privacy">Конфиденциальность</a> · <a href="/terms">Условия</a> · <a href="/uchebnik">Учебник по облигациям</a></p>
</div></footer>
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
  <a class="btn" href="/app?auth=register">Создать портфель бесплатно</a>
</div>
{related_html}
"""
    return _page_shell(title, meta_desc, url, jsonld, body, og_type="article")


@router.get("/bond/{secid}", response_class=HTMLResponse)
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


# ── Catalog ──────────────────────────────────────────────────────────────────

@router.get("/bond", response_class=HTMLResponse)
async def bonds_catalog() -> HTMLResponse:
    """Public catalog of all traded MOEX bonds — crawl entry point for /bond/{secid}."""
    global _catalog_cache
    now = time.time()
    if _catalog_cache and now - _catalog_cache[1] < _CATALOG_TTL:
        return HTMLResponse(_catalog_cache[0], headers=_PUBLIC_CACHE)

    bonds = await _catalog_bonds()
    ofz = sorted((b for b in bonds if b["board"] == "TQOB"), key=lambda b: b.get("maturity") or "")
    corp = sorted((b for b in bonds if b["board"] != "TQOB"), key=lambda b: b["name"])

    def _section(title: str, items: list[dict]) -> str:
        if not items:
            return ""
        rows = "".join(
            f'<tr><td><a href="/bond/{e(b["ticker"])}">{e(b["name"])}</a></td>'
            f'<td>{e(b["ticker"])}</td>'
            f'<td class="num">{_fmt_money(b["price"])}%</td>'
            f'<td class="num">{_fmt_money(b.get("market_yield")) or "—"}%</td>'
            f'<td class="num">{e(b.get("maturity") or "—")}</td></tr>'
            for b in items
        )
        return (f"<h2>{e(title)} ({len(items)})</h2>"
                f'<table class="cat-table"><thead><tr><th>Облигация</th><th>Тикер</th>'
                f"<th>Цена</th><th>Доходность</th><th>Погашение</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    today_str = _fmt_date(date.today())
    body = f"""
<nav class="crumbs"><a href="/">Главная</a> / Облигации</nav>
<h1>Облигации Московской биржи: цены и доходность</h1>
<p class="sub">{len(bonds)} торгуемых выпусков · данные MOEX на {e(today_str or "")}</p>
<p>Каталог облигаций, торгующихся на Московской бирже: ОФЗ и корпоративные выпуски с текущей ценой,
доходностью к погашению и датой погашения. Откройте страницу облигации, чтобы увидеть купонный календарь,
ставку купона, НКД, оферту и кредитный рейтинг. Незнакомые термины — в
<a href="/uchebnik">учебнике по облигациям</a>.</p>
<div class="cta-box">
  <h2>Соберите портфель из этих облигаций</h2>
  <p>Bond AI бесплатно посчитает доходность, покажет купонный календарь и предупредит об офертах.</p>
  <a class="btn" href="/app?auth=register">Начать бесплатно</a>
</div>
{_section("ОФЗ — облигации федерального займа", ofz)}
{_section("Корпоративные облигации", corp)}
"""
    jsonld = [{
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Облигации Московской биржи: каталог с ценами и доходностью",
        "url": _BASE_URL + "/bond",
        "description": "Каталог торгуемых облигаций MOEX: ОФЗ и корпоративные выпуски — цены, доходность к погашению, купоны и даты погашения.",
        "inLanguage": "ru",
    }]
    html = _page_shell(
        "Облигации MOEX: каталог с ценами и доходностью | Bond AI",
        f"Каталог из {len(bonds)} облигаций Московской биржи: ОФЗ и корпоративные — текущие цены, доходность к погашению (YTM), купоны, оферты и рейтинги.",
        _BASE_URL + "/bond", jsonld, body,
    )
    if bonds:
        _catalog_cache = (html, now)
    return HTMLResponse(html, headers=_PUBLIC_CACHE)


# ── Sitemap ──────────────────────────────────────────────────────────────────

@router.get("/sitemap-bonds.xml")
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
