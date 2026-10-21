"""Публичные страницы разбора ОФЗ: /ofz (хаб), /ofz/{номер}, /sitemap-ofz.xml.

Страница строится из SQLite (ofz_daily / ofz_curve_daily), а не из живого
snapshot: цифры фиксируются «по итогам торгов» последней даты в БД. Из сети
берутся только description.json и bondization.json (кэш в памяти 12 ч,
single-flight); при сбое страница рендерится без этих блоков и живёт в кэше
недолго. Список выпусков даёт `ofz_warm.get_selection()` (правило отбора, а не
список в коде); выпуск вне отбора — 404.
"""
import asyncio
import json
import logging
import re
import time
from datetime import date
from html import escape as e

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api import ofz_charts as charts
from app.api.bond_pages import (
    _BASE_URL,
    _PUBLIC_CACHE,
    _freq_text,
    _fmt_money,
    _page_shell,
    _register_href,
    _render_404,
    error_page_html,
)
from app.services import ofz_analytics as oa
from app.services import ofz_commentary as oc
from app.services import ofz_history_service as ohs
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-ofz"])

ROBOTS_SITEMAP_LINE = "Sitemap: https://bondai.ru/sitemap-ofz.xml\n"
LLMS_TXT_LINES = (
    "- [Разборы ОФЗ](https://bondai.ru/ofz): Выпуски ОФЗ с самой торгуемой части доски TQOB — "
    "у каждого своя страница /ofz/{номер}: динамика доходности и цены за год, положение на кривой "
    "бескупонной доходности, спред к кривой, сценарная таблица изменения цены, сравнение с выпусками "
    "близкой дюрации\n",
)

_NUMBER_RE = re.compile(r"^\d{5}$")
_SECID_LONG_RE = re.compile(r"^SU\d{5}RMFS\d$")

_PAGE_TTL = 3600           # страница собрана целиком
_DEGRADED_TTL = 300        # без description/bondization: пересобрать скоро
_STALE_TTL = 24 * 3600     # сколько отдавать протухшую копию, пока её обновляет сосед
_META_TTL = 12 * 3600      # description / bondization
_FETCH_TIMEOUT = 25        # сек; nginx отрезает на 60

_ofz_page_cache: dict = {}   # key -> (html, stored_at, ttl)
_ofz_locks: dict = {}
_meta_cache: dict = {}       # (what, secid) -> (value, stored_at)
_meta_locks: dict = {}

_KIND_LABEL = {"fixed": "ОФЗ-ПД", "float": "ОФЗ-ПК", "linker": "ОФЗ-ИН", "amort": "ОФЗ-АД"}
_KIND_NOTE = {
    "fixed": "постоянный купон",
    "float": "переменный купон",
    "linker": "индексируемый номинал",
    "amort": "номинал погашается частями",
}
_MINUS = "−"


# ── Форматирование ───────────────────────────────────────────────────────────

def _n(v, d: int = 2) -> str:
    s = _fmt_money(v, d) if v is not None else None
    if s is None:
        return "—"
    if s.startswith("-"):
        # «-0,3» при d=0 округляется в «-0»: знак у нуля не нужен
        return s[1:] if not s[1:].replace("0", "").replace(",", "").strip() else _MINUS + s[1:]
    return s


def _sg(v, d: int = 0) -> str:
    """Число со знаком: +16 / −16."""
    if v is None:
        return "—"
    s = _n(v, d)
    return s if s.startswith(_MINUS) or s == "—" or not s.replace("0", "").replace(",", "").strip() else "+" + s


def _dmy(iso) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "—"


def _dm_y(iso) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%y")
    except (TypeError, ValueError):
        return "—"


def _years(days) -> float | None:
    return None if not days else float(days) / 365.0


def _table(headers: list, rows: list, num_from: int = 1) -> str:
    """Таблица в обёртке с горизонтальной прокруткой; ячейки уже экранированы."""
    head = "".join(
        f'<th class="num">{h}</th>' if i >= num_from else f"<th>{h}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td class="num">{c}</td>' if i >= num_from else f"<td>{c}</td>"
            for i, c in enumerate(r)
        ) + "</tr>"
        for r in rows
    )
    return (f'<div class="tbl-wrap"><table class="cat-table"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


# ── Селекция выпусков ────────────────────────────────────────────────────────

async def _selection() -> list:
    """Выпуски отбора из ofz_warm; недоступен/сбой → []."""
    try:
        from app.services import ofz_warm
        sel = await ofz_warm.get_selection()
    except Exception as exc:
        logger.warning("ofz_pages: selection unavailable: %s", exc)
        return []
    return [s for s in (sel or []) if s.get("number") and s.get("secid")]


# ── description / bondization: кэш 12 ч + single-flight ─────────────────────

async def _cached_fetch(what: str, secid: str, fn):
    key = (what, secid)
    hit = _meta_cache.get(key)
    if hit and time.time() - hit[1] < _META_TTL:
        return hit[0]
    lock = _meta_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _meta_cache.get(key)
        if hit and time.time() - hit[1] < _META_TTL:
            return hit[0]
        try:
            val = await asyncio.wait_for(fn(secid), timeout=_FETCH_TIMEOUT)
        except Exception as exc:
            logger.warning("ofz_pages: %s %s failed: %s", what, secid, exc or type(exc).__name__)
            return hit[0] if hit else None
        _meta_cache[key] = (val, time.time())
        return val


# ── Данные из SQLite ─────────────────────────────────────────────────────────

def _load_db(secid: str, kinds: dict) -> dict | None:
    """Всё, что нужно странице выпуска, из БД. kinds: {secid: kind} отбора."""
    st = storage_service
    hist = st.get_ofz_history(secid, 365)
    if not hist:
        return None
    last_date = hist[-1]["tradedate"]
    board = [dict(r, kind=kinds[r["secid"]]) for r in st.get_ofz_board_day(last_date)
             if r["secid"] in kinds]
    nodes = st.get_ofz_curve(last_date)
    curve_by_date: dict = {}
    for period, _ in nodes:
        for d, v in st.get_ofz_curve_history(period, 365):
            curve_by_date.setdefault(d, []).append((period, v))
    return {
        "hist": hist, "last_date": last_date, "board": board, "nodes": nodes,
        "curve_by_date": curve_by_date,
        "window": st.get_ofz_window(20),
    }


# ── Расчёты ──────────────────────────────────────────────────────────────────

def _flows(bond: dict | None, settle: date) -> list:
    """Будущие потоки на одну бумагу [(t лет, ₽)]: купоны и погашения номинала."""
    if not bond:
        return []
    out = []
    for item in (bond.get("coupons") or []) + (bond.get("amortizations") or []):
        d, v = item.get("date"), item.get("value")
        if not d or not v:
            continue
        try:
            t = (date.fromisoformat(d) - settle).days / 365.0
        except ValueError:
            continue
        if t > 0:
            out.append((t, float(v)))
    return out


def _accrued_rub(bond: dict | None, settle: date) -> float:
    """НКД по купонному периоду из графика; нет данных — 0 (чистая цена, без поправки)."""
    for c in (bond or {}).get("coupons") or []:
        try:
            end = date.fromisoformat(c["date"])
            start = date.fromisoformat(c["startdate"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > settle and c.get("value") and end > start and start <= settle:
            return float(c["value"]) * (settle - start).days / (end - start).days
    return 0.0


def _compute(sec: dict, db: dict, desc: dict | None, bond: dict | None, sel: list) -> dict:
    kind = sec["kind"]
    hist, ldate = db["hist"], db["last_date"]
    last = hist[-1]
    settle = date.fromisoformat(ldate)
    price = last.get("close")
    ytm = last.get("yieldclose") or None
    dur_days = last.get("duration")
    face = last.get("facevalue") or sec.get("facevalue") or 1000.0
    price_rub = price * face / 100.0 if price is not None else None

    nodes = db["nodes"]
    spread = None
    if kind in ("fixed", "amort") and ytm and nodes:
        spread = oa.spread_bp(ytm, dur_days, nodes)
    sp_series = []
    if kind in ("fixed", "amort"):
        sp_series = oa.spread_series(hist, db["curve_by_date"])
    sp_median = oa.median([v for _, v in sp_series]) if len(sp_series) >= 2 else None

    price_series = [(r["tradedate"], r["close"]) for r in hist if r.get("close") is not None]
    ytm_series = [(r["tradedate"], r["yieldclose"]) for r in hist if r.get("yieldclose")]
    rng = oa.range_stats(price_series, 365) if price_series else None

    # Доходность MOEX — эффективная годовая, поэтому freq=1 и в MD, и в выпуклости.
    md = oa.modified_duration(dur_days, ytm, 1) if ytm and kind in ("fixed", "amort") else None
    flows = _flows(bond, settle) if kind in ("fixed", "amort") else []
    aci = _accrued_rub(bond, settle) if flows else 0.0
    conv = None
    if flows and ytm and price_rub:
        pv = oa.price_from_cashflows(flows, ytm, 1)
        # Потоки из bondization — независимый источник; если их цена сильно
        # расходится с котировкой, выпуклости им доверять нельзя.
        if abs(pv / (price_rub + aci) - 1.0) <= 0.05:
            conv = oa.convexity_from_cashflows(flows, ytm, 1)
    scen = []
    if md and price is not None:
        scen = oa.price_scenarios(price, face, md, conv, aci_pct=aci / face * 100.0)
    by_shift = {r["shift_bp"]: r for r in scen}

    secids = [s["secid"] for s in sel]
    place, total, med_value = oa.liquidity_rank(sec["secid"], db["window"], 20, universe=secids)

    mat = desc.get("matdate") if desc else None
    mat = mat or sec.get("matdate")
    ytmat = None
    if mat:
        try:
            ytmat = (date.fromisoformat(mat) - settle).days / 365.0
        except ValueError:
            pass

    coupons = (bond or {}).get("coupons") or []
    amorts = (bond or {}).get("amortizations") or []
    future_c = [c for c in coupons if c.get("date") and c["date"] > ldate]
    past_c = [c for c in coupons if c.get("date") and c["date"] <= ldate and c.get("value") is not None]
    future_a = [a for a in amorts if a.get("date") and a["date"] > ldate]

    c = {
        "sec": sec, "kind": kind, "db": db, "desc": desc or {}, "bond": bond,
        "ldate": ldate, "last": last, "price": price, "price_rub": price_rub, "face": face,
        "ytm": ytm, "dur_days": dur_days, "dur_years": _years(dur_days), "md": md, "conv": conv,
        "spread": spread, "sp_series": sp_series, "sp_median": sp_median,
        "price_series": price_series, "ytm_series": ytm_series, "range": rng,
        "scen": scen, "flows": flows, "mat": mat, "ytmat": ytmat,
        "future_c": future_c, "past_c": past_c, "future_a": future_a,
        "liq": (place, total, med_value),
        "coupon_pct": sec.get("couponpercent") or (desc or {}).get("couponpercent"),
    }
    c["facts"] = _facts(c, by_shift)
    return c


def _facts(c: dict, by_shift: dict) -> dict:
    sec, kind, last = c["sec"], c["kind"], c["last"]
    f = {
        "secid": sec["secid"], "number": sec["number"], "kind": kind,
        "close": c["price"], "years_to_maturity": c["ytmat"],
        "liq_rank": c["liq"][0], "liq_total": c["liq"][1],
        "liq_value_mln": c["liq"][2] / 1e6 if c["liq"][2] else None,
    }
    if c["range"]:
        r = c["range"]
        f.update(min52=r["min"], max52=r["max"], pos52=r["pos"])
    if kind in ("fixed", "amort"):
        f.update(spread_bp=c["spread"], spread_median_bp=c["sp_median"], md=c["md"],
                 coupon_pct=c["coupon_pct"], ytm=c["ytm"])
        up, dn = by_shift.get(100), by_shift.get(-100)
        if up and dn:
            f.update(dp_up100_pct=up["dp_pct"], dp_down100_pct=dn["dp_pct"],
                     conv_up_minus_down_pp=(dn["dp_pct"] + up["dp_pct"]) if c["conv"] else None,
                     p0_rub=c["price_rub"], p_up100_rub=up["price_rub_new"],
                     p_down100_rub=dn["price_rub_new"])
    if kind == "linker":
        f.update(real_yield=last.get("iricpiclose"), bei=last.get("beiclose"))
    if kind == "float":
        d = c["desc"]
        f.update(benchmark=d.get("coupon_benchmark"), benchmark_spread_pp=d.get("coupon_benchmark_spread"),
                 last_coupon_pct=c["past_c"][-1].get("valueprc") if c["past_c"] else None)
    if kind == "amort" and c["future_a"]:
        a = c["future_a"][0]
        f.update(next_amort_date=a["date"], amort_share_pct=a.get("valueprc"))
    return f


# ── Разметка: блоки страницы ─────────────────────────────────────────────────

_CSS = """<style>
@media(max-width:640px){.ofz-chart svg .ofz-ax{font-size:19px !important}}
.tbl-wrap{overflow-x:auto;margin:0 0 8px;max-width:100%}
.tbl-wrap table{min-width:100%}
.tbl-wrap th.num,.tbl-wrap td.num{text-align:right;white-space:nowrap}
.tbl-wrap td{white-space:nowrap}
.ofz-chart{margin:0 0 22px;background:var(--panel);border:1px solid var(--line-2);
border-radius:var(--radius-lg);padding:14px 14px 8px}
.ofz-chart h3{font-size:15px;font-weight:700;color:var(--head);margin:0 0 4px}
.ofz-chart figcaption p{font-size:13px;line-height:1.55;color:var(--muted);margin:0 0 8px}
.ofz-chart svg{display:block;max-width:100%;height:auto}
.ofz-prose p{margin-bottom:12px}
.ofz-disc{font-size:12.5px;color:var(--muted);line-height:1.65;border-top:1px solid var(--line-2);
padding-top:14px;margin-top:30px}
.ofz-link{margin:-10px 0 24px;font-size:14px}
.ofz-hub td a{font-weight:600}
@media(max-width:480px){.ofz-chart{padding:10px 8px 4px}}
</style>"""


def _kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="kpi"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value {cls}">{value}</div>'
            + (f'<div class="kpi-sub">{sub}</div>' if sub else "") + "</div>")


def _kpis(c: dict) -> str:
    kind, last, p = c["kind"], c["last"], c["price"]
    out = [_kpi("Цена", f"{_n(p)}%", f"{_n(c['price_rub'])} ₽" if c["price_rub"] else "")]
    if kind == "linker":
        out.append(_kpi("Реальная доходность", f"{_n(last.get('iricpiclose'))}%", "к индексированному номиналу", "green")
                   if last.get("iricpiclose") else "")
        out.append(_kpi("Вменённая инфляция", f"{_n(last.get('beiclose'))}%", "BEI, % годовых")
                   if last.get("beiclose") else "")
        out.append(_kpi("Индексированный номинал", f"{_n(c['face'])} ₽") if last.get("facevalue") else "")
    elif c["ytm"]:
        out.append(_kpi("Доходность (YTM)", f"{_n(c['ytm'])}%", "расчёт MOEX" if kind == "float" else "эффективная годовая", "green"))
    if c["dur_years"]:
        sub = f"модифицированная {_n(c['md'])}" if c["md"] else ""
        out.append(_kpi("Дюрация", f"{_n(c['dur_years'])} г.", sub))
    if c["spread"] is not None:
        out.append(_kpi("Спред к кривой", f"{_sg(c['spread'])} б.п.", "доходность − кривая на той же дюрации"))
    if kind == "float" and c["past_c"] and c["past_c"][-1].get("valueprc") is not None:
        out.append(_kpi("Последний купон", f"{_n(c['past_c'][-1]['valueprc'])}%", "годовых"))
    v = c["last"].get("value")
    if v:
        _, _, med = c["liq"]
        out.append(_kpi("Оборот за день", f"{_n(v / 1e6, 0 if v >= 1e7 else 1)} млн ₽",
                        f"медиана 20 дн.: {_n(med / 1e6, 0 if med >= 1e7 else 1)} млн ₽" if med else ""))
    return '<div class="kpis">' + "".join(x for x in out if x) + "</div>"


def _profile(c: dict) -> str:
    sec, d, kind = c["sec"], c["desc"], c["kind"]
    rows = [("Тип", f"{_KIND_LABEL[kind]}: {_KIND_NOTE[kind]}")]
    freq = _freq_text(sec.get("couponperiod"))
    if kind == "float":
        bm, sp = d.get("coupon_benchmark"), d.get("coupon_benchmark_spread")
        if bm:
            rows.append(("Ставка купона", f"{bm} + {_n(sp)} п.п." if sp is not None else bm))
        if freq:
            rows.append(("Выплаты", freq))
    elif c["coupon_pct"]:
        base = "годовых от индексированного номинала" if kind == "linker" else "годовых"
        rows.append(("Ставка купона", f"{_n(c['coupon_pct'])}% {base}" + (f", {freq}" if freq else "")))
    if c["mat"]:
        extra = f" (через {_n(c['ytmat'], 1)} г.)" if c["ytmat"] and c["ytmat"] > 0 else ""
        rows.append(("Погашение", f"{_dmy(c['mat'])}{extra}"))
    if sec.get("nextcoupon"):
        rows.append(("Ближайший купон", _dmy(sec["nextcoupon"])))
    if d.get("issuedate"):
        rows.append(("Дата размещения", _dmy(d["issuedate"])))
    if d.get("issuesize") and d.get("initialfacevalue"):
        rows.append(("Объём выпуска", f"{_n(d['issuesize'] * d['initialfacevalue'] / 1e9, 1)} млрд ₽ по первоначальному номиналу"))
    rows.append(("Номинал сейчас", f"{_n(c['face'])} ₽"))
    tbl = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in rows)
    return (f'<h2>Профиль выпуска</h2><table class="params">{tbl}</table>'
            f'<p class="ofz-link"><a href="/bond/{e(sec["secid"])}">Все параметры, НКД и график купонов — в карточке облигации →</a></p>')


def _chart_fig(title: str, note: str, svg: str) -> str:
    return (f'<figure class="ofz-chart"><figcaption><h3>{e(title)}</h3><p>{e(note)}</p></figcaption>'
            f"{svg}</figure>")


def _charts(c: dict) -> str:
    kind, last = c["kind"], c["last"]
    figs = []
    if kind == "linker":
        if len(c["price_series"]) >= 2:
            figs.append(_chart_fig("Цена за год", "Котировка в процентах от индексированного номинала.",
                                   charts.line_chart(c["price_series"], title="Цена, % номинала")))
        real = [(r["tradedate"], r["iricpiclose"]) for r in c["db"]["hist"] if r.get("iricpiclose")]
        bei = [(r["tradedate"], r["beiclose"]) for r in c["db"]["hist"] if r.get("beiclose")]
        if len(real) >= 2:
            figs.append(_chart_fig("Реальная доходность за год", "Доходность сверх индексации номинала, % годовых (расчёт MOEX).",
                                   charts.line_chart(real, title="Реальная доходность, %")))
        if len(bei) >= 2:
            figs.append(_chart_fig("Вменённая инфляция (BEI) за год", "Инфляция, при которой доходность линкера равна доходности сопоставимой облигации без индексации.",
                                   charts.line_chart(bei, title="Вменённая инфляция, %")))
        return "".join(figs)
    if len(c["ytm_series"]) >= 2:
        figs.append(_chart_fig("Доходность к погашению за год", "Значение YIELDCLOSE по итогам каждого торгового дня.",
                               charts.line_chart(c["ytm_series"], title="Доходность к погашению, %")))
    if len(c["price_series"]) >= 2:
        figs.append(_chart_fig("Цена за год", "Цена закрытия в процентах от номинала.",
                               charts.line_chart(c["price_series"], title="Цена, % номинала")))
    if kind in ("fixed", "amort") and c["ytm"] and c["dur_years"] and len(c["db"]["nodes"]) >= 2:
        points = [
            {"label": r.get("number") and f"ОФЗ {r['number']}", "d_years": r["duration"] / 365.0, "y": r["yieldclose"]}
            for r in c["db"]["board"]
            if (r["kind"] == "fixed" or r["secid"] == c["sec"]["secid"]) and r.get("duration") and r.get("yieldclose")
        ]
        hl = {"label": f"ОФЗ {c['sec']['number']}", "d_years": c["dur_years"], "y": c["ytm"]}
        figs.append(_chart_fig("Положение на кривой доходности",
                               "Линия — кривая бескупонной доходности ОФЗ (MOEX), точки — выпуски отбора по дюрации и доходности.",
                               charts.curve_chart(c["db"]["nodes"], points, hl)))
    if len(c["sp_series"]) >= 2:
        figs.append(_chart_fig("Спред к кривой за год",
                               "Доходность выпуска минус доходность кривой на его дюрации, б.п.; пунктир — медиана за период.",
                               charts.spread_chart(c["sp_series"], median=c["sp_median"])))
    return "".join(figs)


def _kind_blocks(c: dict) -> str:
    kind, last, d = c["kind"], c["last"], c["desc"]
    if kind == "linker":
        init = d.get("initialfacevalue")
        rows = []
        if last.get("facevalue"):
            rows.append(("Индексированный номинал", f"{_n(last['facevalue'])} ₽"))
            if init:
                rows.append(("Первоначальный номинал", f"{_n(init)} ₽"))
                rows.append(("Коэффициент индексации", f"{_n(last['facevalue'] / init, 3)}"))
        if last.get("iricpiclose"):
            rows.append(("Реальная доходность", f"{_n(last['iricpiclose'])}% годовых"))
        if last.get("beiclose"):
            rows.append(("Вменённая инфляция (BEI)", f"{_n(last['beiclose'])}% годовых"))
        if not rows:
            return ""
        tbl = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in rows)
        return ("<h2>Индексация номинала</h2>"
                "<p>Номинал линкера растёт вместе с инфляцией, поэтому его доходность считают относительно индексированного "
                "номинала и не сравнивают с кривой доходности обычных ОФЗ.</p>"
                f'<table class="params">{tbl}</table>')
    if kind == "float":
        bm, sp = d.get("coupon_benchmark"), d.get("coupon_benchmark_spread")
        txt = []
        if bm:
            txt.append(f"Купон привязан к {e(bm)}" + (f", надбавка — {e(_n(sp))} п.п." if sp is not None else "."))
        if c["past_c"] and c["past_c"][-1].get("valueprc") is not None:
            pc = c["past_c"][-1]
            txt.append(f"Последний установленный купон — {_n(pc['valueprc'])}% годовых ({_n(pc['value'])} ₽), выплата {_dmy(pc['date'])}.")
        if not c["ytm"]:
            txt.append("Доходность к погашению MOEX для этого выпуска не рассчитывает, поэтому на странице она не приводится.")
        if not txt:
            return ""
        return "<h2>Плавающий купон</h2><p>" + " ".join(txt) + "</p>"
    return ""


def _neighbors_block(c: dict) -> str:
    kind = c["kind"]
    if kind == "linker":
        kf = ("linker",)
    elif kind in ("fixed", "amort"):
        kf = ("fixed", "amort")
    else:
        return ""
    nb = oa.neighbors(c["sec"]["secid"], c["db"]["board"], k=4, kind_filter=kf)
    if not nb:
        return ""
    nodes = c["db"]["nodes"]
    lk = kind == "linker"   # спред к номинальной кривой для линкера не считается и не показывается
    rows = []
    for r in nb:
        num = e(r["number"])
        row = [f'<a href="/ofz/{num}">ОФЗ {num}</a>' + ("" if r["kind"] == kind and kind == "fixed" else f' · {e(_KIND_LABEL[r["kind"]][4:])}'),
               _n(r.get("close")), _n((r.get("iricpiclose") or r.get("yieldclose")) if lk else r.get("yieldclose")),
               _n(_years(r.get("duration")))]
        if not lk:
            sp = oa.spread_bp(r.get("yieldclose"), r.get("duration"), nodes) if nodes else None
            row.append(_sg(sp) if sp is not None else "—")
        rows.append(row)
    me = [f"<b>ОФЗ {e(c['sec']['number'])}</b>" + ("" if kind == "fixed" else f" · {e(_KIND_LABEL[kind][4:])}"), _n(c["price"]),
          _n(c["last"].get("iricpiclose") if lk else c["ytm"]), _n(c["dur_years"])]
    if not lk:
        me.append(_sg(c["spread"]) if c["spread"] is not None else "—")
    heads = ["Выпуск", "Цена, %", "Реальная доходность, %" if lk else "Доходность, %", "Дюрация, лет"]
    if not lk:
        heads.append("Спред, б.п.")
    return ("<h2>Выпуски с близкой дюрацией</h2>"
            "<p>Ближайшие по дюрации выпуски отбора по итогам того же торгового дня.</p>"
            + _table(heads, [me] + rows))


def _scenario_block(c: dict) -> str:
    if not c["scen"]:
        return ""
    rows = []
    for r in sorted(c["scen"], key=lambda r: r["shift_bp"]):
        rows.append([f"{_sg(r['shift_bp'] / 100.0, 0)} п.п.", f"{_sg(r['dp_pct'], 2)}%",
                     f"{_n(r['price_pct_new'])}%", f"{_n(r['price_rub_new'])} ₽", f"{_sg(r['dp_rub'], 2)} ₽"])
    method = ("С учётом выпуклости (потоки из графика выплат MOEX)." if c["conv"]
              else "Линейное приближение по модифицированной дюрации: график выплат недоступен, выпуклость не учтена.")
    return ("<h2>Сценарии изменения доходности</h2>"
            "<p>Модельный расчёт при параллельном сдвиге доходности на указанную величину, остальные условия не меняются. "
            f"{method} Это расчёт по формуле, а не оценка будущей цены.</p>"
            + _table(["Сдвиг доходности", "Изменение цены", "Цена, % номинала", "Цена, ₽", "Изменение, ₽"], rows))


def _coupons_block(c: dict) -> str:
    kind = c["kind"]
    bond = c["bond"]
    if not bond:
        return ""
    out = []
    if kind == "float":
        hist_c = c["past_c"][-6:]
        if hist_c:
            items = [(_dm_y(x["date"]), x["value"]) for x in hist_c]
            rows = [[_dmy(x["date"]), f"{_n(x['value'])} ₽", f"{_n(x.get('valueprc'))}%"] for x in hist_c]
            out.append("<h2>Выплаты купонов</h2>"
                       "<p>Последние выплаты по выпуску. Размер будущих купонов определяется по ставке бенчмарка "
                       "в течение купонного периода и заранее не известен.</p>"
                       + _chart_fig("Последние купоны, ₽ за бумагу", "Фактически выплаченные суммы.",
                                    charts.bars_chart(items, title="Последние купоны, ₽", unit="₽"))
                       + _table(["Дата выплаты", "Сумма за бумагу", "Ставка годовых"], rows))
        return "".join(out)
    nxt = [x for x in c["future_c"] if x.get("value")][:6]
    if not nxt:
        return ""
    year_end = date.fromisoformat(c["ldate"]).toordinal() + 365
    annual = sum(x["value"] for x in c["future_c"]
                 if x.get("value") and date.fromisoformat(x["date"]).toordinal() <= year_end)
    items = [(_dm_y(x["date"]), x["value"]) for x in nxt]
    rows = [[_dmy(x["date"]), f"{_n(x['value'])} ₽", f"{_n(x.get('valueprc'))}%"] for x in nxt]
    cur = ""
    if annual and c["price_rub"]:
        cur = (f" Сумма купонов за ближайшие 12 месяцев — {_n(annual)} ₽; к цене {_n(c['price_rub'])} ₽ (без НКД) "
               f"это текущая доходность {_n(annual / c['price_rub'] * 100)}% годовых.")
    return ("<h2>Ближайшие выплаты</h2>"
            f"<p>Купоны по графику выплат MOEX.{e(cur)}</p>"
            + _chart_fig("Ближайшие купоны, ₽ за бумагу", "Суммы выплат по графику.",
                         charts.bars_chart(items, title="Ближайшие купоны, ₽", unit="₽"))
            + _table(["Дата выплаты", "Сумма за бумагу", "Ставка годовых"], rows))


def _amort_block(c: dict) -> str:
    if c["kind"] != "amort" or not c["future_a"]:
        return ""
    fa = c["future_a"]
    items = [(_dm_y(a["date"]), a["value"]) for a in fa if a.get("value")]
    rows = [[_dmy(a["date"]), f"{_n(a.get('valueprc'), 0)}%", f"{_n(a.get('value'))} ₽"] for a in fa]
    return ("<h2>График амортизации</h2>"
            "<p>Часть номинала погашается в указанные даты; после каждой выплаты номинал и размер купонов уменьшаются.</p>"
            + (_chart_fig("Погашения номинала, ₽ за бумагу", "Ближайшие и последующие выплаты номинала.",
                          charts.bars_chart(items, title="Погашения номинала, ₽", unit="₽")) if items else "")
            + _table(["Дата", "Доля номинала", "Сумма за бумагу"], rows))


# ── Страница выпуска ─────────────────────────────────────────────────────────

def _meta_description(c: dict) -> str:
    sec, kind = c["sec"], c["kind"]
    name = sec.get("shortname") or f"ОФЗ {sec['number']}"
    bits = [f"цена {_n(c['price'])}% номинала"]
    if kind == "linker":
        if c["last"].get("iricpiclose"):
            bits.append(f"реальная доходность {_n(c['last']['iricpiclose'])}%")
    elif c["ytm"]:
        bits.append(f"доходность {_n(c['ytm'])}%")
    if c["dur_years"]:
        bits.append(f"дюрация {_n(c['dur_years'])} г.")
    if c["spread"] is not None:
        bits.append(f"спред к кривой {_sg(c['spread'])} б.п.")
    return (f"{name} ({sec['secid']}): " + ", ".join(bits) + f" по итогам торгов {_dmy(c['ldate'])}. "
            "Динамика за год, положение на кривой, сценарии изменения цены и сравнение с соседними выпусками.")


def _render_page(c: dict) -> str:
    sec, kind = c["sec"], c["kind"]
    num, secid = sec["number"], sec["secid"]
    name = sec.get("shortname") or f"ОФЗ {num}"
    url = f"{_BASE_URL}/ofz/{num}"
    meta = _meta_description(c)
    topics = {"linker": "реальная доходность, инфляция, дюрация", "float": "купон, цена, дюрация"}.get(
        kind, "доходность, дюрация, спред к кривой")
    title = f"{name}: разбор выпуска — {topics} | Bond AI"

    comment = oc.build_comment(c["facts"])
    faq = oc.build_faq(c["facts"])
    date_label = f"{_dmy(c['ldate'])} (итоги торгов)"

    body = _CSS + f"""
<nav class="crumbs"><a href="/">Главная</a> / <a href="/bond">Облигации</a> / <a href="/ofz">ОФЗ</a> / {e(num)}</nav>
<h1>{e(name)}: разбор выпуска</h1>
<p class="sub">{e(_KIND_LABEL[kind])} · {e(secid)} · по итогам торгов {e(_dmy(c['ldate']))} · данные Московской биржи</p>
{_kpis(c)}
{_profile(c)}
{_kind_blocks(c)}
<h2>Динамика и положение среди выпусков</h2>
{_charts(c)}
{_neighbors_block(c)}
{_scenario_block(c)}
{_coupons_block(c)}
{_amort_block(c)}
"""
    if comment:
        body += '<h2>Что показывают данные</h2><div class="ofz-prose">' + "".join(f"<p>{e(s)}</p>" for s in comment) + "</div>"
    if faq:
        body += "<h2>Вопросы и ответы</h2>" + "".join(
            f'<div class="faq-item"><h3>{e(q)}</h3><p>{e(a)}</p></div>' for q, a in faq)
    body += f"""
<div class="cta-box">
  <h2>Следите за {e(name)} в своём портфеле</h2>
  <p>Bond AI бесплатно отслеживает цены, купоны и доходность ваших облигаций: купонный календарь,
  Telegram-уведомления о выплатах, импорт портфеля из Т-Банка.</p>
  <a class="btn btn-primary" href="{e(_register_href(secid))}">Создать портфель бесплатно</a>
</div>
<p class="ofz-disc" data-disclaimer="1">{e(oc.disclaimer(date_label))}</p>
"""
    jsonld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Главная", "item": _BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Облигации", "item": _BASE_URL + "/bond"},
            {"@type": "ListItem", "position": 3, "name": "ОФЗ", "item": _BASE_URL + "/ofz"},
            {"@type": "ListItem", "position": 4, "name": num, "item": url},
        ]},
        {"@context": "https://schema.org", "@type": "FinancialProduct",
         "name": name, "alternateName": secid, "category": "Облигация федерального займа",
         "url": url, "description": meta},
    ]
    if faq:
        jsonld.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq]})
    return _page_shell(title, meta, url, jsonld, body, register_href=_register_href(secid))


async def _build_page(sec: dict, sel: list):
    """(html, ttl) или None, если в БД нет истории выпуска."""
    kinds = {s["secid"]: s["kind"] for s in sel}
    numbers = {s["secid"]: s["number"] for s in sel}
    db = await asyncio.to_thread(_load_db, sec["secid"], kinds)
    if db is None:
        return None
    for r in db["board"]:
        r["number"] = numbers[r["secid"]]
    desc, bond = await asyncio.gather(
        _cached_fetch("description", sec["secid"], ohs.fetch_description),
        _cached_fetch("bondization", sec["secid"], ohs.fetch_bondization),
    )
    c = _compute(sec, db, desc, bond, sel)
    ttl = _PAGE_TTL if (desc is not None and bond is not None) else _DEGRADED_TTL
    return _render_page(c), ttl


# ── Хаб ──────────────────────────────────────────────────────────────────────

def _render_hub(sel: list, last_date: str, board: dict, nodes: list) -> str:
    rows = []
    counts: dict = {}
    for s in sel:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
        r = board.get(s["secid"]) or {}
        kind = s["kind"]
        sp = oa.spread_bp(r.get("yieldclose"), r.get("duration"), nodes) if kind in ("fixed", "amort") and nodes else None
        yld = r.get("yieldclose") if kind != "linker" else None
        v = r.get("value")
        num = e(s["number"])
        rows.append([f'<a href="/ofz/{num}">{e(s.get("shortname") or "ОФЗ " + s["number"])}</a>',
                     e(_KIND_LABEL[kind]), _n(r.get("close")), _n(yld) if yld else "—",
                     _n(_years(r.get("duration"))), _sg(sp) if sp is not None else "—",
                     _n(v / 1e6, 0 if v >= 1e7 else 1) if v else "—"])
    kinds_txt = ", ".join(f"{_KIND_LABEL[k]} — {counts[k]}" for k in ("fixed", "linker", "amort", "float") if k in counts)
    title = "Разборы ОФЗ: доходность, дюрация и спред к кривой по выпускам | Bond AI"
    meta = (f"{len(sel)} выпусков ОФЗ с данными по итогам торгов {_dmy(last_date)}: цена, доходность, дюрация, "
            "спред к кривой бескупонной доходности и оборот. У каждого выпуска — отдельный разбор.")
    body = _CSS + f"""
<nav class="crumbs"><a href="/">Главная</a> / <a href="/bond">Облигации</a> / ОФЗ</nav>
<h1>Разборы ОФЗ</h1>
<p class="sub">{len(sel)} выпусков ({e(kinds_txt)}) · по итогам торгов {e(_dmy(last_date))} · данные Московской биржи</p>
<div class="ofz-hub">{_table(["Выпуск", "Тип", "Цена, %", "Доходность, %", "Дюрация, лет", "Спред, б.п.", "Оборот, млн ₽"], rows, num_from=2)}</div>
<h2>О разборах</h2>
<p>Разбор выпуска — страница с динамикой доходности и цены за год, положением выпуска на кривой бескупонной
доходности ОФЗ, спредом к кривой, сценарной таблицей изменения цены и сравнением с выпусками близкой дюрации.
Страницы формируются автоматически, без участия автора, из итогов торгов на доске TQOB Московской биржи; кривая
берётся из данных MOEX (индикатор zcyc). Показатели относятся к дате последних итогов торгов и не обновляются
в течение дня.</p>
<p>В список входят рублёвые выпуски, которые торгуются регулярно: сделки не менее чем в 15 из последних 20 торговых
дней и медиана не менее 20 сделок в день. Все прошедшие порог ОФЗ-ПД, ОФЗ-ИН и ОФЗ-АД включаются, оставшиеся места
занимают ОФЗ-ПК с наибольшим оборотом. Юаневые выпуски в список не входят. Состав пересчитывается ежедневно.</p>
<p>Спред к кривой — разность доходности выпуска и доходности кривой на его дюрации, в базисных пунктах; для линкеров
и флоатеров он не считается, потому что их доходность нельзя сравнивать с кривой по обычным ОФЗ напрямую.
В таблице выпуски сгруппированы по типу и упорядочены по обороту.</p>
<p class="ofz-disc" data-disclaimer="1">{e(oc.disclaimer(_dmy(last_date) + " (итоги торгов)"))}</p>
"""
    jsonld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Главная", "item": _BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Облигации", "item": _BASE_URL + "/bond"},
            {"@type": "ListItem", "position": 3, "name": "ОФЗ", "item": _BASE_URL + "/ofz"},
        ]},
        {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": f"{_BASE_URL}/ofz/{s['number']}",
             "name": s.get("shortname") or f"ОФЗ {s['number']}"}
            for i, s in enumerate(sel)]},
    ]
    return _page_shell(title, meta, _BASE_URL + "/ofz", jsonld, body)


async def _build_hub(sel: list):
    def load():
        last = storage_service.get_ofz_last_date()
        if not last:
            return None
        board = {r["secid"]: r for r in storage_service.get_ofz_board_day(last)}
        return last, board, storage_service.get_ofz_curve(last)
    got = await asyncio.to_thread(load)
    if got is None:
        return None
    last, board, nodes = got
    return _render_hub(sel, last, board, nodes), _PAGE_TTL


# ── Кэш: TTL + stale-while-revalidate + single-flight ───────────────────────

def _unavailable() -> HTMLResponse:
    html = error_page_html("⏳", "Данные обновляются",
                           "Разбор выпуска временно недоступен: данные Московской биржи ещё загружаются. Загляните через несколько минут.",
                           cta_label="Каталог облигаций", cta_href="/bond", title="Данные обновляются | Bond AI")
    return HTMLResponse(html, status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "300"})


async def _serve(key: str, build) -> HTMLResponse:
    now = time.time()
    cached = _ofz_page_cache.get(key)
    if cached and now - cached[1] < cached[2]:
        return HTMLResponse(cached[0], headers=_PUBLIC_CACHE)
    lock = _ofz_locks.setdefault(key, asyncio.Lock())
    if lock.locked() and cached and now - cached[1] < _STALE_TTL:
        # Пересборку ведёт сосед — протухшая копия лучше ожидания.
        return HTMLResponse(cached[0], headers=_PUBLIC_CACHE)
    async with lock:
        fresh = _ofz_page_cache.get(key)
        if fresh and time.time() - fresh[1] < fresh[2]:
            return HTMLResponse(fresh[0], headers=_PUBLIC_CACHE)
        try:
            built = await build()
        except Exception:
            logger.exception("ofz_pages: build %s failed", key)
            built = None
    if built is None:
        if cached and time.time() - cached[1] < _STALE_TTL:
            return HTMLResponse(cached[0], headers=_PUBLIC_CACHE)
        return _unavailable()
    html, ttl = built
    _ofz_page_cache[key] = (html, time.time(), ttl)
    return HTMLResponse(html, headers=_PUBLIC_CACHE)


# ── Роуты ────────────────────────────────────────────────────────────────────

@router.api_route("/ofz", response_class=HTMLResponse, methods=["GET", "HEAD"])
async def ofz_hub() -> HTMLResponse:
    sel = await _selection()
    if not sel:
        return _unavailable()
    return await _serve("hub", lambda: _build_hub(sel))


@router.api_route("/ofz/{number}", response_class=HTMLResponse, methods=["GET", "HEAD"])
async def ofz_page(number: str):
    number = number.upper()
    is_secid = bool(_SECID_LONG_RE.fullmatch(number))
    if not is_secid and not _NUMBER_RE.fullmatch(number):
        return _render_404()   # до обращений к БД и MOEX
    sel = await _selection()
    if not sel:
        return _unavailable()
    if is_secid:
        hit = next((s for s in sel if s["secid"] == number), None)
        if not hit:
            return _render_404()
        return RedirectResponse(f"/ofz/{hit['number']}", status_code=301,
                                headers={"Cache-Control": "public, max-age=3600"})
    sec = next((s for s in sel if s["number"] == number), None)
    if not sec:
        return _render_404()
    return await _serve(number, lambda: _build_page(sec, sel))


# ── Sitemap ──────────────────────────────────────────────────────────────────

async def ofz_urls() -> list:
    """Абсолютные URL хаба и страниц отбора; отбор недоступен → []."""
    sel = await _selection()
    if not sel:
        return []
    return [f"{_BASE_URL}/ofz"] + [f"{_BASE_URL}/ofz/{s['number']}" for s in sel]


@router.api_route("/sitemap-ofz.xml", methods=["GET", "HEAD"])
async def sitemap_ofz() -> Response:
    urls = await ofz_urls()
    lastmod = await asyncio.to_thread(storage_service.get_ofz_last_date) if urls else None
    lm = f"<lastmod>{e(lastmod)}</lastmod>" if lastmod else ""
    entries = "".join(
        f"  <url><loc>{e(u)}</loc>{lm}<changefreq>daily</changefreq><priority>0.6</priority></url>\n"
        for u in urls
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{entries}</urlset>\n")
    return Response(xml, media_type="application/xml", headers=_PUBLIC_CACHE)
