"""
Server-side in-memory cache for portfolio table rows, keyed by portfolio_id.
Background task refreshes MOEX data periodically so that endpoints return instantly.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.models import InstrumentMetrics

logger = logging.getLogger(__name__)


@dataclass
class PortfolioCache:
    """State for a single portfolio's cache."""

    rows: list[InstrumentMetrics] = field(default_factory=list)
    rows_by_id: dict[int, InstrumentMetrics] = field(default_factory=dict)
    last_refresh: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CacheService:
    def __init__(self) -> None:
        self._caches: dict[int, PortfolioCache] = {}  # portfolio_id -> cache
        self._refresh_task: asyncio.Task[None] | None = None
        self.refresh_interval: int = 300  # seconds

    def get_cache(self, portfolio_id: int) -> PortfolioCache:
        """Get or create cache for a portfolio."""
        if portfolio_id not in self._caches:
            self._caches[portfolio_id] = PortfolioCache()
        return self._caches[portfolio_id]

    def rows(self, portfolio_id: int) -> list[InstrumentMetrics]:
        """Get cached rows for a portfolio."""
        cache = self.get_cache(portfolio_id)
        return list(cache.rows)

    def last_refresh(self, portfolio_id: int) -> float:
        """Get last refresh time for a portfolio."""
        cache = self.get_cache(portfolio_id)
        return cache.last_refresh

    def is_warm(self, portfolio_id: int) -> bool:
        """Check if cache is warm for a portfolio."""
        cache = self.get_cache(portfolio_id)
        return cache.last_refresh > 0

    async def refresh(self, portfolio_id: int) -> list[InstrumentMetrics]:
        """Fetch fresh data from MOEX and merge into cache for a portfolio.

        Only overwrites a row if the new fetch was successful
        (current_price > 0).  Keeps old good data for rows that
        failed this time.
        """
        from app.services.portfolio_service import portfolio_service
        from app.services.notification_service import notification_service

        cache = self.get_cache(portfolio_id)

        async with cache.lock:
            old_rows = list(cache.rows)
            try:
                new_rows = await portfolio_service.get_table_fresh(portfolio_id)
                merged = self._merge(new_rows, cache.rows_by_id)
                cache.rows = merged
                cache.rows_by_id = {r.id: r for r in merged}
                cache.last_refresh = time.time()
                ok_count = sum(1 for r in merged if r.current_price > 0)
                logger.info(
                    "Cache refreshed [portfolio_id=%d]: %d/%d rows OK",
                    portfolio_id,
                    ok_count,
                    len(merged),
                )
                # Save daily snapshot if there is real data
                if ok_count > 0:
                    try:
                        from app.services.storage_service import storage_service
                        total_value = sum(r.current_value or 0 for r in merged)
                        total_cost = sum((r.purchase_price or 0) * (r.quantity or 0) for r in merged)
                        storage_service.save_portfolio_snapshot(portfolio_id, total_value, total_cost)
                    except Exception:
                        logger.exception("Failed to save portfolio snapshot for portfolio_id=%d", portfolio_id)
                # Check for rating/price changes and notify
                if old_rows:
                    try:
                        await notification_service.check_and_notify(
                            old_rows, merged
                        )
                    except Exception:
                        logger.exception("Notification check failed")
            except Exception:
                logger.exception("Cache refresh failed for portfolio_id=%d", portfolio_id)
                if not cache.rows:
                    raise
        return list(cache.rows)

    def _merge(
        self, new_rows: list[InstrumentMetrics],
        rows_by_id: dict[int, InstrumentMetrics]
    ) -> list[InstrumentMetrics]:
        """Merge new rows with existing cache:
        - keep old good row if new row has current_price == 0
        - always accept new row if it has real data
        - remove rows no longer in DB
        """
        result: list[InstrumentMetrics] = []
        for new_row in new_rows:
            old_row = rows_by_id.get(new_row.id)
            if (
                new_row.current_price == 0
                and old_row is not None
                and old_row.current_price > 0
            ):
                # Keep old successful data, but update editable fields
                update_data = {
                    "quantity": new_row.quantity,
                    "purchase_price": new_row.purchase_price,
                    "current_value": round(
                        old_row.current_price * new_row.quantity, 2
                    ),
                    "profit": round(
                        (old_row.current_price - new_row.purchase_price)
                        * new_row.quantity,
                        2,
                    ),
                    "coupon": new_row.coupon,
                    "manual_coupon_set": new_row.manual_coupon_set,
                }
                # Preserve bond-specific fields if they exist in new_row
                if new_row.type == "bond":
                    update_data["coupon_rate"] = new_row.coupon_rate
                    update_data["coupon_period"] = new_row.coupon_period
                    update_data["maturity_date"] = new_row.maturity_date
                    update_data["offer_date"] = new_row.offer_date
                    update_data["aci"] = new_row.aci
                    update_data["market_yield"] = new_row.market_yield
                # Preserve stock-specific fields
                if new_row.type == "stock":
                    update_data["dividend_yield"] = new_row.dividend_yield

                patched = old_row.model_copy(update=update_data)
                result.append(patched)
            else:
                result.append(new_row)
        return result

    def invalidate(self, portfolio_id: int) -> None:
        """Mark cache stale for a portfolio so the next periodic tick refreshes early."""
        cache = self.get_cache(portfolio_id)
        cache.last_refresh = 0.0

    def start_background(self) -> None:
        """Start background refresh task."""
        logger.info("Starting background cache refresh (interval: %ds)", self.refresh_interval)
        self._refresh_task = asyncio.create_task(self._background_loop())

    def stop_background(self) -> None:
        """Stop background refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
            logger.debug("Background cache refresh stopped")

    async def _background_loop(self) -> None:
        """Background loop that refreshes all cached portfolios periodically."""
        while True:
            try:
                portfolio_ids = list(self._caches.keys())
                if portfolio_ids:
                    async def _refresh_one(pid: int) -> None:
                        try:
                            await self.refresh(pid)
                        except Exception:
                            logger.exception(
                                "Background refresh error for portfolio_id=%d", pid
                            )
                    await asyncio.gather(*(_refresh_one(pid) for pid in portfolio_ids))
            except Exception:
                logger.exception("Background cache refresh error")
            await asyncio.sleep(self.refresh_interval)


cache_service = CacheService()
