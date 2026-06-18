"""Realized-coupon lookup via MOEX bondization (coupon schedule).

For a bond bought on a given date we want the coupons that have actually been
paid out between the purchase date and today, to compute a proper "full profit"
(price change + realized coupons) for manually-added bonds — the same number we
already get from the T-Bank operations journal for synced portfolios.

Source: https://iss.moex.com/iss/securities/{secid}/bondization.json
The `coupons` block lists every coupon with its `coupondate` and `value` (the
coupon amount per ONE bond, in the bond's face currency). We sum `value` for all
coupon dates in the half-open window (purchase_date, today]. The caller multiplies
by quantity (and applies FX for non-RUB bonds, like the rest of the app).

Results are cached in-memory: a bond's full coupon schedule rarely changes, so a
long TTL is fine. Failures are cached briefly so a transient MOEX hiccup doesn't
get pinned forever.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class _ScheduleCache:
    OK_TTL = 12 * 3600   # 12h — coupon schedules are near-static
    MISS_TTL = 600       # 10min for errors / empty, so we retry soon

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, list[tuple[date, float]] | None]] = {}

    def get(self, secid: str):
        entry = self._store.get(secid)
        if not entry:
            return None, False
        ts, value = entry
        ttl = self.OK_TTL if value else self.MISS_TTL
        if time.time() - ts > ttl:
            return None, False
        return value, True

    def set(self, secid: str, value) -> None:
        self._store[secid] = (time.time(), value)


class CouponScheduleService:
    def __init__(self) -> None:
        self._cache = _ScheduleCache()

    async def _fetch_schedule(self, secid: str) -> list[tuple[date, float]] | None:
        """Return [(coupon_date, value_per_bond), ...] or None on failure."""
        base = settings.moex_base_url.rstrip("/")
        url = f"{base}/securities/{secid}/bondization.json?iss.meta=off&limit=100"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - never propagate to a request
            logger.warning("coupon schedule fetch failed secid=%s: %s", secid, exc)
            return None

        coup = data.get("coupons", {})
        cols = coup.get("columns", [])
        rows = coup.get("data", [])
        if "coupondate" not in cols or "value" not in cols:
            return []
        di, vi = cols.index("coupondate"), cols.index("value")
        out: list[tuple[date, float]] = []
        for row in rows:
            try:
                d = date.fromisoformat(str(row[di])[:10])
            except (ValueError, TypeError, IndexError):
                continue
            try:
                v = float(row[vi]) if row[vi] is not None else 0.0
            except (ValueError, TypeError):
                v = 0.0
            out.append((d, v))
        return out

    async def _get_schedule(self, secid: str) -> list[tuple[date, float]]:
        cached, hit = self._cache.get(secid)
        if hit:
            return cached or []
        schedule = await self._fetch_schedule(secid)
        self._cache.set(secid, schedule)  # cache None/[] too (short TTL)
        return schedule or []

    async def realized_coupons_per_bond(
        self, secid: str, since: date, until: date | None = None
    ) -> float:
        """Sum of coupons (per one bond) paid in (since, until]. until defaults to today.

        Half-open on the lower bound: a coupon paid exactly on the purchase date
        went to the previous holder, so it's excluded.
        """
        if not secid or since is None:
            return 0.0
        until = until or date.today()
        schedule = await self._get_schedule(secid)
        return round(sum(v for d, v in schedule if since < d <= until), 4)


coupon_schedule_service = CouponScheduleService()
