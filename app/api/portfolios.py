"""
Portfolio management API: CRUD operations and sharing.
"""

import csv
import io
import time
import uuid

import bcrypt
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_portfolio_or_403
from app.config import settings as app_settings
from app.services.cache_service import cache_service
from app.services.cbr_service import cbr_service
from app.services.portfolio_service import portfolio_service
from app.services.storage_service import storage_service

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


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
    password: str | None = Field(None, min_length=1, max_length=100)
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
    if count >= app_settings.max_portfolios_per_user:
        raise HTTPException(status_code=400, detail=f"Максимум {app_settings.max_portfolios_per_user} портфелей на аккаунт")
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

    # 4) Free cash (RUB equivalent) — from portfolio_sync
    free_cash_rub = 0.0
    try:
        cfg = storage_service.get_sync_config(portfolio_id)
        if cfg and cfg.get("cash_balance"):
            import json as _json
            cash_list = _json.loads(cfg["cash_balance"])
            from app.services.moex_service import moex_service as _moex
            for c in cash_list:
                ccy = (c.get("currency") or "").upper()
                amt = float(c.get("amount") or 0)
                if ccy in ("RUB", "SUR", ""):
                    rate = 1.0
                else:
                    rate = await _moex._get_fx_rate(ccy) or 0.0
                free_cash_rub += amt * rate
    except Exception:
        pass

    # 5) Key rate
    key_rate = await cbr_service.get_key_rate()

    return {
        "events": events,
        "anomalies": anomalies,
        "realized_coupons": round(realized_coupons, 2),
        "free_cash_rub": round(free_cash_rub, 2),
        "key_rate": key_rate,
    }


# ── Aggregated endpoints across all user's portfolios ───────────────────────


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


@router.get("/all/table")
async def get_all_table(current_user: dict = Depends(get_current_user)) -> dict:
    """Aggregated table: union of items across all user's portfolios with rebalanced weights."""
    user_id = current_user["sub"]
    rows, _portfolios, origin = await _collect_all_user_rows(user_id)

    total_value = sum(float(r.current_value or 0) for r in rows) or 1.0
    items: list[dict] = []
    for r in rows:
        d = r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__)
        d["weight"] = round(float(r.current_value or 0) / total_value * 100, 2)
        pid, pname = origin.get(id(r), (None, None))
        d["portfolio_id"] = pid
        d["portfolio_name"] = pname
        items.append(d)
    return {"items": items}


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
                agg[d] = {"total_value": 0.0, "total_cost": 0.0}
            agg[d]["total_value"] += float(s.get("total_value") or 0)
            agg[d]["total_cost"] += float(s.get("total_cost") or 0)

    return [
        {"date": d, "total_value": round(v["total_value"], 2), "total_cost": round(v["total_cost"], 2)}
        for d, v in sorted(agg.items())
    ]


@router.get("/all/analytics-extra")
async def get_all_analytics_extra(current_user: dict = Depends(get_current_user)) -> dict:
    """Aggregated analytics-extra across all user's portfolios."""
    from datetime import date, datetime, timedelta

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

    # 4) Free cash across all portfolios
    free_cash_rub = 0.0
    for p in portfolios_data:
        try:
            cfg = storage_service.get_sync_config(p["id"])
            if cfg and cfg.get("cash_balance"):
                import json as _json
                cash_list = _json.loads(cfg["cash_balance"])
                from app.services.moex_service import moex_service as _moex
                for c in cash_list:
                    ccy = (c.get("currency") or "").upper()
                    amt = float(c.get("amount") or 0)
                    if ccy in ("RUB", "SUR", ""):
                        rate = 1.0
                    else:
                        rate = await _moex._get_fx_rate(ccy) or 0.0
                    free_cash_rub += amt * rate
        except Exception:
            continue

    key_rate = await cbr_service.get_key_rate()

    return {
        "events": events,
        "anomalies": anomalies,
        "realized_coupons": round(realized_coupons, 2),
        "free_cash_rub": round(free_cash_rub, 2),
        "key_rate": key_rate,
    }
