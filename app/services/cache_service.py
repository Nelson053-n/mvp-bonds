"""
Server-side in-memory cache for portfolio table rows.
Background task refreshes MOEX data periodically so that
/portfolio/table returns instantly.
"""

import asyncio
import logging
import time

from app.models import InstrumentMetrics

logger = logging.getLogger(__name__)


class CacheService:
    def __init__(self) -> None:
        self._rows: list[InstrumentMetrics] = []
        self._rows_by_id: dict[int, InstrumentMetrics] = {}
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self.refresh_interval: int = 120  # seconds

    @property
    def rows(self) -> list[InstrumentMetrics]:
        return list(self._rows)

    @property
    def last_refresh(self) -> float:
        return self._last_refresh

    @property
    def is_warm(self) -> bool:
        return self._last_refresh > 0

    async def refresh(self) -> list[InstrumentMetrics]:
        """Fetch fresh data from MOEX and merge into cache.

        Only overwrites a row if the new fetch was successful
        (current_price > 0).  Keeps old good data for rows that
        failed this time.
        """
        from app.services.portfolio_service import portfolio_service

        async with self._lock:
            try:
                new_rows = await portfolio_service.get_table_fresh()
                merged = self._merge(new_rows)
                self._rows = merged
                self._rows_by_id = {r.id: r for r in merged}
                self._last_refresh = time.time()
                ok_count = sum(
                    1 for r in merged if r.current_price > 0
                )
                logger.info(
                    "Cache refreshed: %d/%d rows OK",
                    ok_count,
                    len(merged),
                )
            except Exception:
                logger.exception("Cache refresh failed")
                if not self._rows:
                    raise
        return list(self._rows)

    def _merge(
        self, new_rows: list[InstrumentMetrics]
    ) -> list[InstrumentMetrics]:
        """Merge new rows with existing cache:
        - keep old good row if new row has current_price == 0
        - always accept new row if it has real data
        - remove rows no longer in DB
        """
        result: list[InstrumentMetrics] = []
        for new_row in new_rows:
            old_row = self._rows_by_id.get(new_row.id)
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

    def invalidate(self) -> None:
        """Mark cache stale so the next periodic tick refreshes early."""
        self._last_refresh = 0.0

    def start_background(self) -> None:
        logger.info("Starting background cache refresh (interval: %ds)", self.refresh_interval)
        self._refresh_task = asyncio.create_task(self._background_loop())

    def stop_background(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
            logger.debug("Background cache refresh stopped")

    async def _background_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Background cache refresh error")
            await asyncio.sleep(self.refresh_interval)


cache_service = CacheService()
