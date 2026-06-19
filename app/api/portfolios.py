"""
Portfolio management API: CRUD operations and sharing.
"""

import csv
import io
import re
import time
import uuid

import bcrypt
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_portfolio_or_403, user_is_pro
from app.config import settings as app_settings
from app.services.cache_service import cache_service
from app.services.cbr_service import cbr_service
from app.services.portfolio_service import portfolio_service
from app.services.storage_service import storage_service

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

# Yields above this (% annual) are almost always yield-to-offer/buyback distorted
# by an imminent put date, not a meaningful YTM — flag them so the huge number
# in the «Рыночная доходность» column is explained rather than looking broken.
_YIELD_ANOMALY_THRESHOLD = 100.0
_MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _yield_to_offer_anomaly(r, today) -> dict | None:
    """If a bond shows an absurdly high yield caused by a near offer/buyback,
    return an anomaly explaining it is yield-to-offer (with the date)."""
    my = getattr(r, "market_yield", None)
    if not my or my <= _YIELD_ANOMALY_THRESHOLD:
        return None
    # Find the nearest future put-style date that explains the distortion.
    near = None
    for fld in ("offer_date", "buyback_date"):
        d = getattr(r, fld, None)
        if d and d >= today and (near is None or d < near):
            near = d
    if near is None:
        return None
    date_str = f"{near.day} {_MONTHS_RU[near.month]} {near.year}"
    return {
        "type": "yield_to_offer",
        "ticker": r.ticker,
        "text": (
            f"{r.ticker}: доходность {my:.0f}% — это доходность к оферте "
            f"{date_str}, а не к погашению"
        ),
        "severity": "medium",
    }


class CreatePortfolioInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class UpdatePortfolioInput(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)


class PortfolioResponse(BaseModel):
    id: int
    user_id: int
    name: str
    share_token: str | None
    has_share_password: bool
    created_at: str


class PortfoliosListResponse(BaseModel):
    portfolios: list[PortfolioResponse]


class SharePortfolioInput(BaseModel):
    password: str | None = Field(None, min_length=8, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class SharePortfolioResponse(BaseModel):
    share_url: str
    share_token: str


@router.get("", response_model=PortfoliosListResponse)
async def list_portfolios(current_user: dict = Depends(get_current_user)) -> dict:
    """List all portfolios for current user."""
    user_id = current_user["sub"]
    portfolios_data = storage_service.get_portfolios(user_id)

    return {
        "portfolios": [
            {
                "id": p["id"],
                "user_id": p["user_id"],
                "name": p["name"],
                "share_token": p["share_token"],
                "has_share_password": p["share_password_hash"] is not None,
                "created_at": p["created_at"],
            }
            for p in portfolios_data
        ]
    }


@router.post("", response_model=PortfolioResponse, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    payload: CreatePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a new portfolio for current user."""
    user_id = current_user["sub"]
    count = storage_service.count_portfolios(user_id)
    # Free users are capped; Pro is unlimited (up to the legacy hard safety net).
    if user_is_pro(current_user):
        limit = app_settings.max_portfolios_per_user
    else:
        limit = app_settings.free_max_portfolios
    if count >= limit:
        if user_is_pro(current_user):
            detail = f"Максимум {limit} портфелей на аккаунт"
        else:
            detail = f"На бесплатном тарифе — до {limit} портфелей. Оформите Pro для большего."
        raise HTTPException(status_code=400, detail=detail)
    portfolio_id = storage_service.create_portfolio(user_id, payload.name)
    portfolio = storage_service.get_portfolio(portfolio_id)

    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.get("/export-all")
async def export_all_portfolios(
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Export all portfolios as a single CSV with portfolio_name column."""
    user_id = current_user["sub"]
    portfolios = storage_service.get_portfolios(user_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["portfolio_name", "ticker", "instrument_type", "quantity", "purchase_price"])
    for p in portfolios:
        items = storage_service.get_items(p["id"])
        for item in items:
            writer.writerow([
                p["name"],
                item["ticker"],
                item["instrument_type"],
                item["quantity"],
                item["purchase_price"],
            ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=all_portfolios.csv"},
    )


@router.post("/import-all")
async def import_all_portfolios(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Import all portfolios from CSV. Missing portfolios are created automatically.

    Expected columns: portfolio_name, ticker, instrument_type, quantity, purchase_price.
    """
    user_id = current_user["sub"]

    _MAX_CSV_SIZE = 2 * 1024 * 1024  # 2 MB для экспорта всех портфелей
    content = await file.read(_MAX_CSV_SIZE + 1)
    if len(content) > _MAX_CSV_SIZE:
        raise HTTPException(status_code=413, detail="Файл слишком большой (максимум 2 МБ)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    required = {"portfolio_name", "ticker", "instrument_type", "quantity", "purchase_price"}

    added = 0
    errors: list[str] = []
    portfolio_cache: dict[str, int] = {}

    for p in storage_service.get_portfolios(user_id):
        portfolio_cache[p["name"]] = p["id"]

    for i, row in enumerate(reader, start=2):
        missing = required - set(row.keys())
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Отсутствуют колонки: {', '.join(missing)}",
            )

        portfolio_name = str(row["portfolio_name"]).strip()
        ticker = str(row["ticker"]).strip().upper()
        instrument_type = str(row["instrument_type"]).strip().lower()

        if not portfolio_name:
            errors.append(f"Строка {i}: пустое название портфеля")
            continue

        if instrument_type not in ("bond", "stock"):
            errors.append(f"Строка {i}: неверный тип '{instrument_type}'")
            continue

        try:
            quantity = float(row["quantity"])
            purchase_price = float(row["purchase_price"])
        except ValueError:
            errors.append(f"Строка {i}: неверные числовые значения")
            continue

        if quantity <= 0 or purchase_price <= 0:
            errors.append(f"Строка {i}: quantity и purchase_price должны быть > 0")
            continue

        if portfolio_name not in portfolio_cache:
            pid = storage_service.create_portfolio(user_id, portfolio_name)
            portfolio_cache[portfolio_name] = pid

        pid = portfolio_cache[portfolio_name]

        try:
            existing_item = storage_service.get_item_by_ticker(pid, ticker, instrument_type)
            if existing_item:
                storage_service.update_item(existing_item["id"], pid, quantity, purchase_price)
            else:
                storage_service.add_item(
                    ticker=ticker,
                    instrument_type=instrument_type,
                    quantity=quantity,
                    purchase_price=purchase_price,
                    portfolio_id=pid,
                )
            added += 1
        except Exception as exc:
            errors.append(f"Строка {i}: {exc}")

    if added > 0:
        for pid in portfolio_cache.values():
            cache_service.invalidate(pid)

    return {"added": added, "errors": len(errors), "error_details": errors}


# ── Aggregated endpoints across all user's portfolios ───────────────────────
# IMPORTANT: must be declared BEFORE /{portfolio_id}/* routes — FastAPI returns
# 422 (not "skip and try next") when "all" fails int validation on
# /{portfolio_id}/snapshots and /{portfolio_id}/analytics-extra.


async def _collect_all_user_rows(user_id: int) -> tuple[list, list[dict], dict]:
    """Fetch tables for every portfolio owned by user.

    Returns (rows, portfolios, origin) where origin maps row id() → (portfolio_id, portfolio_name).
    """
    portfolios_data = storage_service.get_portfolios(user_id)
    all_rows: list = []
    origin: dict = {}
    for p in portfolios_data:
        try:
            rows = await portfolio_service.get_table(p["id"])
        except Exception:
            continue
        for r in rows:
            origin[id(r)] = (p["id"], p["name"])
            all_rows.append(r)
    return all_rows, portfolios_data, origin


def _merge_by_ticker(items: list[dict]) -> list[dict]:
    """Merge same-ticker positions held across different portfolios/brokers into
    one row: sum quantity / current_value / profit / coupons, weighted-average the
    purchase price, list the brokers (portfolios) it sits in. Per-unit fields
    (current_price, coupon, rating, dates…) are identical for the same security,
    so they're taken from the first occurrence."""
    merged: dict[str, dict] = {}
    for d in items:
        key = (d.get("ticker") or "").upper()
        qty = float(d.get("quantity") or 0)
        cur = merged.get(key)
        if cur is None:
            nd = dict(d)
            nd["_cost_sum"] = float(d.get("purchase_price") or 0) * qty
            nd["brokers"] = []
            pname = d.get("portfolio_name")
            if pname:
                nd["brokers"].append({"portfolio_id": d.get("portfolio_id"), "name": pname, "quantity": qty})
            merged[key] = nd
        else:
            cur["quantity"] = float(cur.get("quantity") or 0) + qty
            cur["current_value"] = float(cur.get("current_value") or 0) + float(d.get("current_value") or 0)
            cur["profit"] = float(cur.get("profit") or 0) + float(d.get("profit") or 0)
            if d.get("full_profit") is not None:
                cur["full_profit"] = float(cur.get("full_profit") or 0) + float(d["full_profit"])
            if d.get("realized_coupons") is not None:
                cur["realized_coupons"] = float(cur.get("realized_coupons") or 0) + float(d["realized_coupons"])
            if d.get("day_profit") is not None:
                cur["day_profit"] = float(cur.get("day_profit") or 0) + float(d["day_profit"])
            cur["_cost_sum"] += float(d.get("purchase_price") or 0) * qty
            pname = d.get("portfolio_name")
            if pname:
                cur["brokers"].append({"portfolio_id": d.get("portfolio_id"), "name": pname, "quantity": qty})
    out: list[dict] = []
    for d in merged.values():
        q = float(d.get("quantity") or 0)
        d["purchase_price"] = round(d.pop("_cost_sum") / q, 4) if q else d.get("purchase_price")
        # merged rows are virtual — can't edit/move/delete a specific item
        d["aggregated"] = len(d["brokers"]) > 1
        d["portfolio_id"] = None if d["aggregated"] else d.get("portfolio_id")
        out.append(d)
    return out


@router.get("/all/table")
async def get_all_table(
    group: str = "",
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Aggregated table across all user's portfolios. group=ticker merges the same
    security held at different brokers into one row."""
    user_id = current_user["sub"]
    rows, _portfolios, origin = await _collect_all_user_rows(user_id)

    items: list[dict] = []
    for r in rows:
        d = r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__)
        pid, pname = origin.get(id(r), (None, None))
        d["portfolio_id"] = pid
        d["portfolio_name"] = pname
        items.append(d)

    if group == "ticker":
        items = _merge_by_ticker(items)

    # (re)compute weights on the final row set
    total_value = sum(float(d.get("current_value") or 0) for d in items) or 1.0
    for d in items:
        d["weight"] = round(float(d.get("current_value") or 0) / total_value * 100, 2)
    return {"items": items}


@router.get("/all/totals")
async def get_all_totals(current_user: dict = Depends(get_current_user)) -> dict:
    """Per-portfolio totals across all of user's portfolios in one round-trip.

    Replaces N parallel /portfolios/{id}/table calls the SPA used to do just to
    sum current_value. Reads from portfolio_service which is backed by the
    background MOEX cache, so this is cheap.
    """
    user_id = current_user["sub"]
    portfolios_data = storage_service.get_portfolios(user_id)
    totals: list[dict] = []
    grand_value = 0.0
    for p in portfolios_data:
        try:
            rows = await portfolio_service.get_table(p["id"])
        except Exception:
            rows = []
        p_value = sum(float(getattr(r, "current_value", 0) or 0) for r in rows)
        totals.append({
            "id": p["id"],
            "name": p["name"],
            "total_value": round(p_value, 2),
        })
        grand_value += p_value
    return {
        "totals": totals,
        "grand_total_value": round(grand_value, 2),
    }


@router.get("/all/snapshots")
async def get_all_snapshots(
    days: int = 90,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """Sum daily snapshots across all user's portfolios."""
    if days not in (7, 30, 90, 365):
        days = 90
    user_id = current_user["sub"]
    portfolios_data = storage_service.get_portfolios(user_id)

    agg: dict[str, dict[str, float]] = {}
    for p in portfolios_data:
        snaps = storage_service.get_portfolio_snapshots(p["id"], days)
        for s in snaps:
            d = s["date"]
            if d not in agg:
                agg[d] = {"total_value": 0.0, "total_cost": 0.0, "securities_value": 0.0, "_has_sec": False}
            agg[d]["total_value"] += float(s.get("total_value") or 0)
            agg[d]["total_cost"] += float(s.get("total_cost") or 0)
            if s.get("securities_value") is not None:
                agg[d]["securities_value"] += float(s["securities_value"])
                agg[d]["_has_sec"] = True

    return [
        {"date": d, "total_value": round(v["total_value"], 2), "total_cost": round(v["total_cost"], 2),
         "securities_value": (round(v["securities_value"], 2) if v["_has_sec"] else None)}
        for d, v in sorted(agg.items())
    ]


@router.get("/all/analytics-extra")
async def get_all_analytics_extra(current_user: dict = Depends(get_current_user)) -> dict:
    """Aggregated analytics-extra across all user's portfolios."""
    from datetime import date, datetime, timedelta

    if not user_is_pro(current_user):
        raise HTTPException(status_code=403, detail="Доступно в тарифе Pro")
    user_id = current_user["sub"]
    rows, portfolios_data, _origin = await _collect_all_user_rows(user_id)
    portfolio_ids = [p["id"] for p in portfolios_data]
    today = date.today()

    # 1) Upcoming events (30 days)
    events: list[dict] = []
    horizon = today + timedelta(days=30)
    for r in rows:
        if r.type != "bond":
            continue
        ticker = r.ticker
        name = r.name or ticker
        qty = float(r.quantity or 0)
        coupon = float(r.coupon or 0)
        period = int(r.coupon_period or 0)
        if r.next_coupon_date and period > 0 and coupon > 0 and qty > 0:
            d = r.next_coupon_date
            mat = r.maturity_date
            while d <= horizon:
                if mat and d >= mat:
                    break
                if today <= d <= horizon:
                    events.append({
                        "date": d.isoformat(),
                        "type": "coupon",
                        "ticker": ticker,
                        "name": name,
                        "amount": round(coupon * qty, 2),
                    })
                d = d + timedelta(days=period)
        if r.maturity_date and today <= r.maturity_date <= horizon:
            cv = float(r.current_value or 0)
            aci_total = float(r.aci or 0) * qty
            principal = max(0.0, cv - aci_total)
            events.append({
                "date": r.maturity_date.isoformat(),
                "type": "maturity",
                "ticker": ticker,
                "name": name,
                "amount": round(principal, 2),
            })
        for fld in ("offer_date", "buyback_date"):
            d = getattr(r, fld, None)
            if d and today <= d <= horizon:
                events.append({
                    "date": d.isoformat(),
                    "type": "offer" if fld == "offer_date" else "buyback",
                    "ticker": ticker,
                    "name": name,
                    "amount": 0.0,
                })
    events.sort(key=lambda e: (e["date"], e["type"]))

    # 2) Anomalies
    anomalies: list[dict] = []
    try:
        cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
        with storage_service._connect() as conn:
            tickers = list({r.ticker for r in rows if r.type == "bond"})
            if tickers:
                placeholders = ",".join("?" * len(tickers))
                cursor = conn.execute(
                    f"SELECT ticker, rating, source, recorded_at FROM rating_history "
                    f"WHERE ticker IN ({placeholders}) AND recorded_at >= ? "
                    f"ORDER BY ticker, recorded_at ASC",
                    (*tickers, cutoff),
                )
                from app.services.rating_utils import rating_worsened
                history_by_ticker: dict[str, list[tuple[str, str]]] = {}
                for tkr, rating, _src, ts in cursor.fetchall():
                    history_by_ticker.setdefault(tkr, []).append((rating, ts))
                for tkr, hist in history_by_ticker.items():
                    if len(hist) >= 2 and rating_worsened(hist[0][0], hist[-1][0]):
                        anomalies.append({
                            "type": "rating_downgrade",
                            "ticker": tkr,
                            "text": f"{tkr}: рейтинг снижен {hist[0][0]} → {hist[-1][0]}",
                            "severity": "high",
                        })
    except Exception:
        pass

    try:
        with storage_service._connect() as conn:
            seen_drop: set[str] = set()
            for r in rows:
                if r.type != "bond" or not r.current_price or r.ticker in seen_drop:
                    continue
                cursor = conn.execute(
                    "SELECT price FROM price_snapshots WHERE ticker = ? "
                    "ORDER BY recorded_at DESC LIMIT 5",
                    (r.ticker,),
                )
                prev_rows = cursor.fetchall()
                if len(prev_rows) >= 2:
                    prev = prev_rows[1][0]
                    if prev and prev > 0:
                        diff_pct = (r.current_price - prev) / prev * 100
                        if diff_pct <= -3:
                            anomalies.append({
                                "type": "price_drop",
                                "ticker": r.ticker,
                                "text": f"{r.ticker}: цена {diff_pct:+.1f}% к предыдущему дню",
                                "severity": "medium",
                            })
                            seen_drop.add(r.ticker)
    except Exception:
        pass

    soon = today + timedelta(days=7)
    seen_event: set[tuple[str, str]] = set()
    for r in rows:
        if r.type != "bond":
            continue
        for fld, label in (("maturity_date", "погашение"), ("offer_date", "оферта"), ("buyback_date", "buyback")):
            d = getattr(r, fld, None)
            if d and today < d <= soon and (r.ticker, fld) not in seen_event:
                anomalies.append({
                    "type": "imminent_event",
                    "ticker": r.ticker,
                    "text": f"{r.ticker}: {label} через {(d - today).days} дн.",
                    "severity": "medium",
                })
                seen_event.add((r.ticker, fld))
        ytm_anom = _yield_to_offer_anomaly(r, today)
        if ytm_anom:
            anomalies.append(ytm_anom)

    # 3) Realized coupons across all portfolios
    realized_coupons = 0.0
    if portfolio_ids:
        try:
            with storage_service._connect() as conn:
                placeholders = ",".join("?" * len(portfolio_ids))
                cursor = conn.execute(
                    f"SELECT SUM(amount) FROM coupon_notifications "
                    f"WHERE portfolio_id IN ({placeholders}) AND amount IS NOT NULL",
                    tuple(portfolio_ids),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    realized_coupons = float(row[0])
        except Exception:
            pass

    # 4) Free cash across all portfolios — gather distinct FX rates concurrently,
    #    then fold cash totals; key_rate runs alongside via asyncio.gather.
    async def _compute_free_cash_all() -> float:
        import json as _json
        from app.services.moex_service import moex_service as _moex

        all_cash: list[tuple[str, float]] = []
        for p in portfolios_data:
            try:
                cfg = storage_service.get_sync_config(p["id"])
                if not cfg or not cfg.get("cash_balance"):
                    continue
                for c in _json.loads(cfg["cash_balance"]):
                    ccy = (c.get("currency") or "").upper()
                    amt = float(c.get("amount") or 0)
                    all_cash.append((ccy, amt))
            except Exception:
                continue

        non_rub = {ccy for ccy, _ in all_cash if ccy not in ("RUB", "SUR", "")}
        import asyncio as _aio
        fx_rates = dict(zip(non_rub, await _aio.gather(*(_moex._get_fx_rate(ccy) for ccy in non_rub))))
        total = 0.0
        for ccy, amt in all_cash:
            rate = 1.0 if ccy in ("RUB", "SUR", "") else (fx_rates.get(ccy) or 0.0)
            total += amt * rate
        return total

    import asyncio as _asyncio
    free_cash_rub, key_rate = await _asyncio.gather(
        _compute_free_cash_all(),
        cbr_service.get_key_rate(),
    )

    return {
        "events": events,
        "anomalies": anomalies,
        "realized_coupons": round(realized_coupons, 2),
        "free_cash_rub": round(free_cash_rub, 2),
        "key_rate": key_rate,
    }


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Get portfolio details."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.get("/{portfolio_id}/ai-analysis")
async def portfolio_ai_analysis(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Pro-only deep AI analysis of the whole portfolio (real LLM)."""
    await get_portfolio_or_403(portfolio_id, current_user)
    user = storage_service.get_user_by_id(current_user["sub"])
    if not user or not user.get("is_pro"):
        raise HTTPException(status_code=403, detail="Доступно в тарифе Pro")

    from app.services.llm_service import llm_service
    if not llm_service.real_ai_available:
        return {"available": False,
                "message": "AI-анализ скоро будет доступен.",
                "summary": "", "points": []}

    # Each call hits the paid OpenAI API — cap per-user frequency so a Pro
    # account can't be looped to burn the LLM bill (CWE-770).
    if not storage_service.check_rate_limit(f"ai_analysis:{current_user['sub']}", 3600, 20):
        raise HTTPException(status_code=429, detail="Слишком много запросов AI-анализа. Попробуйте позже.")

    rows = await portfolio_service.get_table(portfolio_id)
    # ticker is the one user-supplied field reaching the LLM — strip anything
    # but alphanumerics/dash so it can't carry prompt-injection text.
    def _safe_ticker(t: str) -> str:
        return re.sub(r"[^A-Za-z0-9-]", "", str(t or ""))[:32]
    holdings = [
        {
            "ticker": _safe_ticker(r.ticker),
            "name": r.name,
            "type": r.type,
            "value_rub": round(r.current_value or 0, 2),
            "weight_pct": None,  # filled below
            "ytm": r.market_yield,
            "rating": r.company_rating,
            "coupon_rate": r.coupon_rate,
            "profit_rub": round(r.profit or 0, 2),
        }
        for r in rows
    ]
    total = sum(h["value_rub"] for h in holdings) or 1.0
    for h in holdings:
        h["weight_pct"] = round(h["value_rub"] / total * 100, 1)

    result = await llm_service.analyze_portfolio(holdings)
    return result


async def _require_pro(portfolio_id: int, current_user: dict):
    """Ownership + Pro gate shared by Pro-only portfolio endpoints."""
    await get_portfolio_or_403(portfolio_id, current_user)
    user = storage_service.get_user_by_id(current_user["sub"])
    if not user or not user.get("is_pro"):
        raise HTTPException(status_code=403, detail="Доступно в тарифе Pro")


@router.get("/{portfolio_id}/tax-report")
async def portfolio_tax_report(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Pro-only: НДФЛ estimate for the portfolio."""
    await _require_pro(portfolio_id, current_user)
    from app.services.tax_service import build_tax_report
    rows = await portfolio_service.get_table(portfolio_id)
    return build_tax_report(rows)


@router.get("/{portfolio_id}/tax-report.csv")
async def portfolio_tax_report_csv(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Pro-only: НДФЛ estimate as a CSV (opens in Excel)."""
    await _require_pro(portfolio_id, current_user)
    from app.services.tax_service import build_tax_report
    rows = await portfolio_service.get_table(portfolio_id)
    report = build_tax_report(rows)

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Тикер", "Название", "Кол-во", "Затраты, ₽", "Стоимость, ₽",
                "Финрезультат, ₽", "Купоны получены, ₽"])
    for p in report["positions"]:
        w.writerow([p["ticker"], p["name"], p["quantity"], p["purchase_cost"],
                    p["current_value"], p["result"], p["coupons_received"]])
    s = report["summary"]
    w.writerow([])
    w.writerow(["Купонный доход", s["coupon_income"], "Налог с купонов", s["coupon_tax"]])
    w.writerow(["Финрезультат (прогноз)", s["financial_result"],
                "Налог с прибыли", s["result_tax"]])
    w.writerow(["Итого база", s["total_base"], "Итого налог (оценка)", s["total_tax"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="tax-report-{portfolio_id}.csv"'},
    )


@router.patch("/{portfolio_id}", response_model=PortfolioResponse)
async def update_portfolio(
    portfolio_id: int,
    payload: UpdatePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Update portfolio name."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    if payload.name:
        storage_service.update_portfolio(portfolio_id, name=payload.name)

    portfolio = storage_service.get_portfolio(portfolio_id)
    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Delete a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)
    storage_service.delete_portfolio(portfolio_id)


@router.post("/{portfolio_id}/refresh-ratings")
async def refresh_portfolio_ratings(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Force a fresh credit-rating fetch for every exchange bond in the portfolio.

    Ratings normally refresh once a day in the background; this lets a user pull
    the latest on demand. Custom (off-exchange) items have no MOEX rating and are
    skipped. Manual ratings (manual_rating) take priority and are never touched.
    """
    import asyncio

    from app.services.moex_service import moex_service

    await get_portfolio_or_403(portfolio_id, current_user)
    # Hits SmartLab per ticker — cap to once per 5 minutes per portfolio.
    if not storage_service.check_rate_limit(f"refresh_ratings:{portfolio_id}", 300, 1):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Рейтинги недавно обновлялись. Попробуйте через несколько минут.",
        )
    tickers = {
        item["ticker"]
        for item in storage_service.get_items(portfolio_id)
        if item.get("ticker") and item.get("source") != "custom"
    }
    updated = 0
    sem = asyncio.Semaphore(3)

    async def _refresh_one(ticker: str) -> None:
        nonlocal updated
        async with sem:
            try:
                result = await moex_service.refresh_rating_with_sources(ticker)
            except Exception:
                return
        if result.get("best") is not None:
            storage_service.update_rating_all_items_for_ticker(ticker, result["best"])
            updated += 1

    await asyncio.gather(*(_refresh_one(t) for t in tickers))
    # Rebuild the cache so the next /table call returns the fresh ratings.
    await cache_service.refresh(portfolio_id)
    return {"updated": updated, "total": len(tickers)}


@router.post("/{portfolio_id}/share", response_model=SharePortfolioResponse)
async def create_share_link(
    portfolio_id: int,
    payload: SharePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a public share link for a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    # Generate unique share token
    share_token = str(uuid.uuid4())

    # Hash password if provided
    share_password_hash = None
    if payload.password:
        share_password_hash = bcrypt.hashpw(
            payload.password.encode(), bcrypt.gensalt()
        ).decode()

    expires_at = int(time.time()) + payload.expires_in_days * 86400 if payload.expires_in_days else None

    storage_service.update_portfolio(
        portfolio_id,
        share_token=share_token,
        share_password_hash=share_password_hash,
        share_expires_at=expires_at,
    )

    return {
        "share_token": share_token,
        "share_url": f"/share/{share_token}",
    }


@router.delete("/{portfolio_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Revoke the public share link for a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)
    storage_service.update_portfolio(
        portfolio_id, share_token=None, share_password_hash=None
    )


@router.post("/{portfolio_id}/merge-into/{target_id}")
async def merge_portfolio(
    portfolio_id: int,
    target_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Move all items from source portfolio into target portfolio."""
    if portfolio_id == target_id:
        raise HTTPException(status_code=400, detail="Нельзя объединить портфель с самим собой")
    await get_portfolio_or_403(portfolio_id, current_user)
    await get_portfolio_or_403(target_id, current_user)
    moved = storage_service.merge_portfolios(portfolio_id, target_id)
    return {"moved": moved}


@router.get("/{portfolio_id}/snapshots")
async def get_snapshots(
    portfolio_id: int,
    days: int = 90,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """Get portfolio value history."""
    await get_portfolio_or_403(portfolio_id, current_user)
    if days not in (7, 30, 90, 365):
        days = 90
    return storage_service.get_portfolio_snapshots(portfolio_id, days)


@router.get("/benchmarks/rgbi")
async def get_rgbi_history(
    days: int = 90,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """Get RGBI (MOEX OFZ bond index) historical values."""
    if days not in (7, 30, 90, 365):
        days = 90
    return storage_service.get_benchmark_snapshots("RGBI", days)


@router.get("/meta/key-rate")
async def get_key_rate(current_user: dict = Depends(get_current_user)) -> dict:
    """Get current Bank of Russia key rate (with 24h cache)."""
    rate = await cbr_service.get_key_rate()
    return {"key_rate": rate}


@router.get("/{portfolio_id}/analytics-extra")
async def get_analytics_extra(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Aggregated analytics: anomalies, upcoming events, realized coupons (approx)."""
    from datetime import date, datetime, timedelta

    await get_portfolio_or_403(portfolio_id, current_user)
    if not user_is_pro(current_user):
        raise HTTPException(status_code=403, detail="Доступно в тарифе Pro")
    rows = await portfolio_service.get_table(portfolio_id)
    today = date.today()

    # 1) Upcoming events (30 days): coupons + maturities + offers
    events: list[dict] = []
    horizon = today + timedelta(days=30)
    for r in rows:
        if r.type != "bond":
            continue
        ticker = r.ticker
        name = r.name or ticker
        qty = float(r.quantity or 0)
        coupon = float(r.coupon or 0)
        period = int(r.coupon_period or 0)
        # Coupons
        if r.next_coupon_date and period > 0 and coupon > 0 and qty > 0:
            d = r.next_coupon_date
            mat = r.maturity_date
            while d <= horizon:
                if mat and d >= mat:
                    break
                if today <= d <= horizon:
                    events.append({
                        "date": d.isoformat(),
                        "type": "coupon",
                        "ticker": ticker,
                        "name": name,
                        "amount": round(coupon * qty, 2),
                    })
                d = d + timedelta(days=period)
        # Maturity
        if r.maturity_date and today <= r.maturity_date <= horizon:
            cv = float(r.current_value or 0)
            aci_total = float(r.aci or 0) * qty
            principal = max(0.0, cv - aci_total)
            events.append({
                "date": r.maturity_date.isoformat(),
                "type": "maturity",
                "ticker": ticker,
                "name": name,
                "amount": round(principal, 2),
            })
        # Offer / buyback
        for fld in ("offer_date", "buyback_date"):
            d = getattr(r, fld, None)
            if d and today <= d <= horizon:
                events.append({
                    "date": d.isoformat(),
                    "type": "offer" if fld == "offer_date" else "buyback",
                    "ticker": ticker,
                    "name": name,
                    "amount": 0.0,
                })
    events.sort(key=lambda e: (e["date"], e["type"]))

    # 2) Anomalies
    anomalies: list[dict] = []
    # 2a) Rating downgrades in last 14 days
    try:
        cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
        with storage_service._connect() as conn:
            tickers = [r.ticker for r in rows if r.type == "bond"]
            if tickers:
                placeholders = ",".join("?" * len(tickers))
                cursor = conn.execute(
                    f"SELECT ticker, rating, source, recorded_at FROM rating_history "
                    f"WHERE ticker IN ({placeholders}) AND recorded_at >= ? "
                    f"ORDER BY ticker, recorded_at ASC",
                    (*tickers, cutoff),
                )
                from app.services.rating_utils import rating_worsened
                history_by_ticker: dict[str, list[tuple[str, str]]] = {}
                for tkr, rating, _src, ts in cursor.fetchall():
                    history_by_ticker.setdefault(tkr, []).append((rating, ts))
                for tkr, hist in history_by_ticker.items():
                    if len(hist) >= 2 and rating_worsened(hist[0][0], hist[-1][0]):
                        anomalies.append({
                            "type": "rating_downgrade",
                            "ticker": tkr,
                            "text": f"{tkr}: рейтинг снижен {hist[0][0]} → {hist[-1][0]}",
                            "severity": "high",
                        })
    except Exception:
        pass

    # 2b) Sharp price drop (>=3% from previous snapshot, last 2 days)
    try:
        with storage_service._connect() as conn:
            for r in rows:
                if r.type != "bond" or not r.current_price:
                    continue
                cursor = conn.execute(
                    "SELECT price FROM price_snapshots WHERE ticker = ? "
                    "ORDER BY recorded_at DESC LIMIT 5",
                    (r.ticker,),
                )
                prev_rows = cursor.fetchall()
                if len(prev_rows) >= 2:
                    prev = prev_rows[1][0]
                    if prev and prev > 0:
                        diff_pct = (r.current_price - prev) / prev * 100
                        if diff_pct <= -3:
                            anomalies.append({
                                "type": "price_drop",
                                "ticker": r.ticker,
                                "text": f"{r.ticker}: цена {diff_pct:+.1f}% к предыдущему дню",
                                "severity": "medium",
                            })
    except Exception:
        pass

    # 2c) Imminent maturity/offer (<7 days)
    soon = today + timedelta(days=7)
    for r in rows:
        if r.type != "bond":
            continue
        for fld, label in (("maturity_date", "погашение"), ("offer_date", "оферта"), ("buyback_date", "buyback")):
            d = getattr(r, fld, None)
            if d and today < d <= soon:
                anomalies.append({
                    "type": "imminent_event",
                    "ticker": r.ticker,
                    "text": f"{r.ticker}: {label} через {(d - today).days} дн.",
                    "severity": "medium",
                })
        ytm_anom = _yield_to_offer_anomaly(r, today)
        if ytm_anom:
            anomalies.append(ytm_anom)

    # 3) Realized coupons approximation: from past coupon_notifications if available
    realized_coupons = 0.0
    try:
        with storage_service._connect() as conn:
            cursor = conn.execute(
                """
                SELECT SUM(amount) FROM coupon_notifications
                WHERE portfolio_id = ? AND amount IS NOT NULL
                """,
                (portfolio_id,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                realized_coupons = float(row[0])
    except Exception:
        pass

    # 4) Free cash (RUB equivalent) — from portfolio_sync.
    #    FX rates are fetched in parallel; key_rate runs alongside via asyncio.gather.
    async def _compute_free_cash() -> float:
        try:
            cfg = storage_service.get_sync_config(portfolio_id)
            if not cfg or not cfg.get("cash_balance"):
                return 0.0
            import json as _json
            cash_list = _json.loads(cfg["cash_balance"])
            from app.services.moex_service import moex_service as _moex
            # Gather distinct non-RUB FX rates concurrently.
            non_rub = {
                (c.get("currency") or "").upper()
                for c in cash_list
                if (c.get("currency") or "").upper() not in ("RUB", "SUR", "")
            }
            import asyncio as _aio
            fx_rates = dict(zip(non_rub, await _aio.gather(*(_moex._get_fx_rate(ccy) for ccy in non_rub))))
            total = 0.0
            for c in cash_list:
                ccy = (c.get("currency") or "").upper()
                amt = float(c.get("amount") or 0)
                rate = 1.0 if ccy in ("RUB", "SUR", "") else (fx_rates.get(ccy) or 0.0)
                total += amt * rate
            return total
        except Exception:
            return 0.0

    import asyncio as _asyncio
    free_cash_rub, key_rate = await _asyncio.gather(
        _compute_free_cash(),
        cbr_service.get_key_rate(),
    )

    return {
        "events": events,
        "anomalies": anomalies,
        "realized_coupons": round(realized_coupons, 2),
        "free_cash_rub": round(free_cash_rub, 2),
        "key_rate": key_rate,
    }


