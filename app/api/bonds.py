import asyncio
import logging
import random
import time
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.services.moex_service import moex_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bonds", tags=["bonds"])

# Ratings accepted per risk level
RISK_RATING_TIERS: dict[str, list[str]] = {
    "ultra_low": [],  # OFZ — no rating filter needed
    "low":       ["AAA", "AA+", "AA", "AA-"],
    "moderate":  ["A+", "A", "A-", "BBB+", "BBB"],
    "elevated":  ["BBB-", "BB+"],
    "high":      ["BB+"],  # hard floor: nothing below BB+
}

# Coupon % range used as fallback when ratings are unavailable
RISK_COUPON_RANGE: dict[str, tuple[float, float]] = {
    "ultra_low": (0.0,  15.0),
    "low":       (0.0,  12.0),
    "moderate":  (9.0,  17.0),
    "elevated":  (14.0, 22.0),
    "high":      (17.0, 26.0),
}

# Global hard floor: never include bonds rated below this
_RATING_ORDER = [
    "AAA", "AA+", "AA", "AA-",
    "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-",
    "B+", "B", "B-",
]
_MIN_ALLOWED_RATING = "BB+"
_MIN_ALLOWED_SCORE = _RATING_ORDER.index(_MIN_ALLOWED_RATING)  # 10

_bonds_cache: list[dict[str, Any]] | None = None
_bonds_cache_ts: float = 0.0
_bonds_cache_lock: asyncio.Lock | None = None  # initialised lazily after event loop starts
BONDS_CACHE_TTL = 3600  # seconds


def _coupon_frequency(period_days: int | None) -> int:
    """Convert coupon period in days to payments per year."""
    if not period_days or period_days <= 0:
        return 2  # default assumption
    if period_days <= 35:
        return 12
    if period_days <= 100:
        return 4
    if period_days <= 200:
        return 2
    return 1


def _parse_date_safe(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except Exception:
        return None


async def _fetch_board(
    client: httpx.AsyncClient, board: str
) -> list[dict[str, Any]]:
    url = (
        f"https://iss.moex.com/iss/engines/stock/markets/bonds"
        f"/boards/{board}/securities.json"
        f"?iss.meta=off&iss.only=securities,marketdata"
        f"&securities.columns=SECID,SHORTNAME,PREVLEGALCLOSEPRICE,"
        f"COUPONPERCENT,COUPONPERIOD,FACEVALUE,MATDATE,OFFERDATE,LOTSIZE"
        f"&marketdata.columns=SECID,YIELD,VALTODAY"
        f"&limit=1000"
    )
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch bonds board=%s: %s", board, exc)
        return []

    sec = data.get("securities", {})
    mkt = data.get("marketdata", {})
    sec_cols: list[str] = sec.get("columns", [])
    mkt_cols: list[str] = mkt.get("columns", [])
    sec_rows: list[list] = sec.get("data", [])
    mkt_rows: list[list] = mkt.get("data", [])

    # Build marketdata lookup {SECID -> row_dict}
    mkt_lookup: dict[str, dict] = {}
    if "SECID" in mkt_cols:
        sidx = mkt_cols.index("SECID")
        for row in mkt_rows:
            if len(row) > sidx:
                mkt_lookup[row[sidx]] = dict(zip(mkt_cols, row))

    today = date.today()
    min_date = today + timedelta(days=180)  # 6 months minimum
    results: list[dict[str, Any]] = []

    for row in sec_rows:
        bond = dict(zip(sec_cols, row))
        secid = bond.get("SECID")
        if not secid:
            continue

        price = bond.get("PREVLEGALCLOSEPRICE")
        coupon = bond.get("COUPONPERCENT")
        face = bond.get("FACEVALUE") or 1000
        lotsize = bond.get("LOTSIZE") or 1
        coupon_period = bond.get("COUPONPERIOD")

        if not price or float(price) <= 0:
            continue
        if not coupon or float(coupon) <= 0:
            continue

        # Maturity must be > 6 months away
        mat_date = _parse_date_safe(bond.get("MATDATE") or "")
        if mat_date and mat_date < min_date:
            continue

        # Offer date: if present, must be > 6 months away
        offer_date = _parse_date_safe(bond.get("OFFERDATE") or "")
        if offer_date and offer_date < min_date:
            continue

        mkt_data = mkt_lookup.get(secid, {})
        market_yield = mkt_data.get("YIELD")

        period_int = int(coupon_period) if coupon_period else None

        results.append({
            "ticker": secid,
            "name": bond.get("SHORTNAME", secid),
            "price": float(price),
            "coupon_percent": float(coupon),
            "face_value": float(face),
            "lot_size": int(lotsize),
            "maturity": bond.get("MATDATE") or None,
            "offer_date": bond.get("OFFERDATE") or None,
            "coupon_period": period_int,
            "coupon_frequency": _coupon_frequency(period_int),
            "market_yield": float(market_yield) if market_yield else float(coupon),
            "board": board,
            "rating": None,
        })

    return results


async def _fetch_all_bonds() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20) as client:
        ofz, corp = await asyncio.gather(
            _fetch_board(client, "TQOB"),
            _fetch_board(client, "TQCB"),
        )
    return ofz + corp


async def _get_bonds_cached() -> list[dict[str, Any]]:
    global _bonds_cache, _bonds_cache_ts, _bonds_cache_lock
    # Initialise lock lazily (requires a running event loop)
    if _bonds_cache_lock is None:
        _bonds_cache_lock = asyncio.Lock()

    now = time.time()
    if _bonds_cache is not None and (now - _bonds_cache_ts) < BONDS_CACHE_TTL:
        return _bonds_cache

    async with _bonds_cache_lock:
        # Re-check inside lock to avoid thundering herd
        if _bonds_cache is not None and (time.time() - _bonds_cache_ts) < BONDS_CACHE_TTL:
            return _bonds_cache

        bonds = await _fetch_all_bonds()
        if bonds:
            _bonds_cache = bonds
            _bonds_cache_ts = time.time()
        elif _bonds_cache is not None:
            return _bonds_cache  # stale cache on error
    return _bonds_cache or []


def _adjust_risk_for_amount(risk: str, amount: float) -> str:
    """Large portfolios get nudged toward safer tiers automatically."""
    if amount >= 10_000_000 and risk in ("elevated", "high"):
        return "moderate"
    if amount >= 5_000_000 and risk == "high":
        return "elevated"
    return risk


def _rating_score(rating: str | None) -> int:
    """Lower = higher quality. Unknown ratings get a neutral score."""
    if not rating:
        return len(_RATING_ORDER)
    clean = rating.upper().split("(")[0].strip()
    try:
        return _RATING_ORDER.index(clean)
    except ValueError:
        return len(_RATING_ORDER)


def _rating_below_floor(rating: str | None) -> bool:
    """Return True if rating is known and worse than BB+."""
    if not rating:
        return False  # unknown rating → allowed
    score = _rating_score(rating)
    # score == len(_RATING_ORDER) means unrecognised → allow
    if score == len(_RATING_ORDER):
        return False
    return score > _MIN_ALLOWED_SCORE


@router.get("/suggest")
async def suggest_portfolio(
    amount: float = Query(..., ge=10_000, le=100_000_000),
    yield_target: float = Query(..., ge=5, le=30, alias="yield"),
    risk: str = Query(..., pattern="^(ultra_low|low|moderate|elevated|high)$"),
    _user: dict = Depends(get_current_user),
) -> dict:
    all_bonds = await _get_bonds_cached()
    if not all_bonds:
        raise HTTPException(502, "Не удалось загрузить данные с MOEX")

    adjusted_risk = _adjust_risk_for_amount(risk, amount)

    # Step 1: board filter
    if adjusted_risk == "ultra_low":
        pool = [b for b in all_bonds if b["board"] == "TQOB"]
    else:
        pool = [b for b in all_bonds if b["board"] == "TQCB"]

    if not pool:
        pool = all_bonds

    # Step 2: yield proximity filter [target-5, target+8]
    y_min = max(0.0, yield_target - 5)
    y_max = yield_target + 8
    filtered = [b for b in pool if y_min <= b["coupon_percent"] <= y_max]
    if len(filtered) < 5:
        filtered = pool  # widen if too few candidates

    # Step 3: sort by proximity to target yield, take top-50 for rating fetch
    filtered.sort(key=lambda b: abs(b["coupon_percent"] - yield_target))
    candidates = filtered[:50]

    # Step 4: fetch ratings in parallel for top candidates
    async def _fetch_rating(bond: dict) -> dict:
        try:
            rating = await moex_service._get_smartlab_credit_rating(bond["ticker"])
            if rating is None:
                rating = await moex_service._get_credit_rating(bond["ticker"])
        except Exception:
            rating = None
        return {**bond, "rating": rating}

    rated: list[dict] = list(
        await asyncio.gather(*[_fetch_rating(b) for b in candidates])
    )

    # Step 5: hard floor — drop bonds with known rating below BB+
    rated = [b for b in rated if not _rating_below_floor(b.get("rating"))]

    # Step 6: filter by rating tier; fall back to coupon range if insufficient
    tier = RISK_RATING_TIERS.get(adjusted_risk, [])
    tier_upper = [r.upper() for r in tier]

    if tier_upper:
        tier_matched = [
            b for b in rated
            if b["rating"] and b["rating"].upper().split("(")[0].strip() in tier_upper
        ]
        if len(tier_matched) >= 3:
            candidates = tier_matched
        else:
            c_min, c_max = RISK_COUPON_RANGE.get(adjusted_risk, (0, 50))
            candidates = [
                b for b in rated
                if c_min <= b["coupon_percent"] <= c_max
            ] or rated
    else:
        candidates = rated

    # Step 7: sort: closest yield first, then best rating
    candidates.sort(key=lambda b: (
        abs(b["coupon_percent"] - yield_target),
        _rating_score(b.get("rating")),
    ))

    # Step 8: budget allocation with weighted random sampling to increase diversity
    max_items = 12
    selected: list[dict] = []
    total_cost = 0.0
    remaining = amount

    # Precompute candidate purchase metadata
    alloc_pool = []
    for bond in candidates:
        lot_price = (bond["price"] / 100.0) * bond["face_value"] * bond["lot_size"]
        if lot_price <= 0:
            continue
        # Estimate lots similarly to previous logic (budget per bond heuristic)
        est_lots = max(1, round((amount / max_items) / lot_price))
        est_cost = est_lots * lot_price
        purchase_price = round((bond["price"] / 100.0) * bond["face_value"], 2)
        alloc_pool.append({
            "bond": bond,
            "lot_price": lot_price,
            "est_lots": est_lots,
            "est_cost": est_cost,
            "purchase_price": purchase_price,
        })

    # Continue selecting while budget allows and we have candidates
    while len(selected) < max_items and alloc_pool and remaining >= 1:
        # Filter feasible candidates given remaining budget (at least one lot)
        feasible = [p for p in alloc_pool if p["lot_price"] <= remaining]
        if not feasible:
            break

        # Compute weights: prefer closer to target yield and better rating; add small randomness
        weights = []
        for p in feasible:
            b = p["bond"]
            yield_diff = abs(b["coupon_percent"] - yield_target)
            yield_score = 1.0 / (1.0 + yield_diff)
            rating_score = 1.0 / (1.0 + _rating_score(b.get("rating") ) / 10.0)
            w = yield_score * rating_score * (1.0 + random.random() * 0.25)
            weights.append(max(w, 1e-6))

        # Choose one candidate by weight (without replacement)
        chosen = random.choices(feasible, weights=weights, k=1)[0]

        # Determine lots to buy for chosen candidate within remaining budget
        lots = chosen["est_lots"]
        cost = lots * chosen["lot_price"]
        if cost > remaining:
            lots = max(1, int(remaining / chosen["lot_price"]))
            cost = lots * chosen["lot_price"]

        # Skip if somehow cost is zero
        if cost <= 0:
            alloc_pool = [p for p in alloc_pool if p is not chosen]
            continue

        bond = chosen["bond"]
        item = {
            "ticker": bond["ticker"],
            "name": bond["name"],
            "rating": bond.get("rating"),
            "coupon_percent": round(bond["coupon_percent"], 2),
            "coupon_frequency": bond["coupon_frequency"],
            "price": round(bond["price"], 2),
            "face_value": int(bond["face_value"]),
            "lot_size": bond["lot_size"],
            "lots": lots,
            "total_cost": round(cost, 2),
            "purchase_price": chosen["purchase_price"],
            "maturity": bond["maturity"],
            "offer_date": bond.get("offer_date"),
            "market_yield": round(bond.get("market_yield") or bond["coupon_percent"], 2),
        }
        selected.append(item)
        total_cost += cost
        remaining = amount - total_cost

        # Remove chosen from pool
        alloc_pool = [p for p in alloc_pool if p is not chosen]

    if not selected:
        raise HTTPException(404, "Не удалось подобрать бумаги под заданные параметры")

    avg_yield = sum(b["coupon_percent"] for b in selected) / len(selected)

    return {
        "bonds": selected,
        "summary": {
            "total_cost": round(total_cost, 2),
            "avg_yield": round(avg_yield, 2),
            "bonds_count": len(selected),
            "risk": adjusted_risk,
            "original_risk": risk,
        },
    }
