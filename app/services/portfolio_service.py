import asyncio
from dataclasses import dataclass
import logging

from app.models import (
    AddInstrumentInput,
    InstrumentMetrics,
    UpdateCouponInput,
    UpdateInstrumentInput,
    ValidationResponse,
)
from app.services.llm_service import llm_service
from app.services.moex_service import moex_service
from app.services.storage_service import storage_service
from app.exceptions import ValidationError, InstrumentNotFoundError

logger = logging.getLogger(__name__)


@dataclass
class PortfolioItem:
    id: int
    ticker: str
    instrument_type: str
    quantity: float
    purchase_price: float
    manual_coupon: float | None


class PortfolioService:
    async def validate(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        return await llm_service.validate_instrument(payload)

    async def add_instrument(
        self, payload: AddInstrumentInput
    ) -> InstrumentMetrics:
        validation = await self.validate(payload)
        if not validation.validated:
            warnings_msg = "; ".join(validation.warnings) or "Ошибка валидации данных"
            logger.warning("Validation failed for %s: %s", payload.ticker, warnings_msg)
            raise ValidationError("Ошибка валидации", warnings_msg)

        ticker = payload.ticker.upper().strip()
        try:
            if validation.instrument_type == "bond":
                await moex_service.get_bond_snapshot(ticker)
            else:
                await moex_service.get_stock_snapshot(ticker)
        except Exception as exc:
            logger.error("Failed to fetch market data for %s: %s", ticker, exc)
            raise

        new_id = storage_service.add_item(
            ticker=ticker,
            instrument_type=validation.instrument_type,
            quantity=payload.quantity,
            purchase_price=payload.purchase_price,
        )
        logger.info("Added instrument %s (ID: %d) to portfolio", ticker, new_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh()
        for row in rows:
            if row.id == new_id:
                return row

        logger.error("Failed to retrieve added instrument %s from cache", ticker)
        raise ValueError(
            "Не удалось сформировать строку для добавленной бумаги"
        )

    def delete_instrument(self, item_id: int) -> bool:
        deleted = storage_service.delete_item(item_id)
        if deleted > 0:
            logger.info("Deleted instrument ID %d from portfolio", item_id)
            from app.services.cache_service import cache_service
            cache_service.invalidate()
        return deleted > 0

    async def update_instrument(
        self,
        item_id: int,
        payload: UpdateInstrumentInput,
    ) -> InstrumentMetrics:
        updated = storage_service.update_item(
            item_id=item_id,
            quantity=payload.quantity,
            purchase_price=payload.purchase_price,
        )
        if updated == 0:
            logger.warning("Update failed: instrument ID %d not found", item_id)
            raise InstrumentNotFoundError(item_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh()
        for row in rows:
            if row.id == item_id:
                logger.info("Updated instrument ID %d", item_id)
                return row

        logger.error("Failed to retrieve updated instrument ID %d", item_id)
        raise ValueError("Не удалось сформировать строку после обновления")

    async def update_coupon(
        self,
        item_id: int,
        payload: UpdateCouponInput,
    ) -> InstrumentMetrics:
        updated = storage_service.update_coupon(
            item_id=item_id,
            coupon=payload.coupon,
        )
        if updated == 0:
            logger.warning("Coupon update failed: instrument ID %d not found", item_id)
            raise InstrumentNotFoundError(item_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh()
        for row in rows:
            if row.id == item_id:
                logger.info("Updated coupon for instrument ID %d", item_id)
                return row

        logger.error("Failed to retrieve updated coupon for instrument ID %d", item_id)
        raise ValueError("Не удалось сформировать строку после обновления")

    async def remove_not_found_instruments(self) -> int:
        stored_items = [
            PortfolioItem(
                id=int(item["id"]),
                ticker=str(item["ticker"]),
                instrument_type=str(item["instrument_type"]),
                quantity=float(item["quantity"]),
                purchase_price=float(item["purchase_price"]),
                manual_coupon=(
                    float(item["manual_coupon"])
                    if item["manual_coupon"] is not None
                    else None
                ),
            )
            for item in storage_service.get_items()
        ]

        missing_ids: list[int] = []
        for item in stored_items:
            try:
                if item.instrument_type == "bond":
                    await moex_service.get_bond_snapshot(item.ticker)
                else:
                    await moex_service.get_stock_snapshot(item.ticker)
            except Exception as exc:
                logger.warning(
                    "Instrument %s (%s) not found on MOEX: %s",
                    item.ticker,
                    item.instrument_type,
                    exc,
                )
                missing_ids.append(item.id)

        if missing_ids:
            deleted_count = storage_service.delete_items(missing_ids)
            logger.info("Removed %d not-found instruments: %s", deleted_count, missing_ids)
            return deleted_count
        return 0

    async def get_table(self) -> list[InstrumentMetrics]:
        """Return cached table (instant). Falls back to fresh fetch."""
        from app.services.cache_service import cache_service

        if cache_service.is_warm:
            return cache_service.rows
        return await cache_service.refresh()

    # ------------------------------------------------------------------
    # Heavy method: called ONLY by cache_service in background
    # ------------------------------------------------------------------
    async def get_table_fresh(self) -> list[InstrumentMetrics]:
        stored_items = [
            PortfolioItem(
                id=int(item["id"]),
                ticker=str(item["ticker"]),
                instrument_type=str(item["instrument_type"]),
                quantity=float(item["quantity"]),
                purchase_price=float(item["purchase_price"]),
                manual_coupon=(
                    float(item["manual_coupon"])
                    if item["manual_coupon"] is not None
                    else None
                ),
            )
            for item in storage_service.get_items()
        ]

        # Limit concurrent MOEX requests to avoid rate-limits/timeouts
        semaphore = asyncio.Semaphore(3)

        async def fetch_row(item: PortfolioItem) -> InstrumentMetrics:
            async with semaphore:
                try:
                    if item.instrument_type == "bond":
                        snapshot = await moex_service.get_bond_snapshot(
                            item.ticker
                        )
                        nominal = snapshot.nominal or 1000.0
                        current_price = (
                            (snapshot.clean_price_percent / 100.0) * nominal
                            + (snapshot.aci or 0.0)
                        )
                        current_value = current_price * item.quantity
                        profit = (
                            (current_price - item.purchase_price)
                            * item.quantity
                        )
                        return InstrumentMetrics(
                            id=item.id,
                            type="bond",
                            name=snapshot.name,
                            ticker=snapshot.ticker,
                            current_price=round(current_price, 4),
                            purchase_price=item.purchase_price,
                            quantity=item.quantity,
                            current_value=round(current_value, 2),
                            profit=round(profit, 2),
                            weight=0.0,
                            company_rating=snapshot.company_rating,
                            coupon=(
                                item.manual_coupon
                                if item.manual_coupon is not None
                                else snapshot.coupon
                            ),
                            coupon_period=snapshot.coupon_period,
                            manual_coupon_set=item.manual_coupon is not None,
                            maturity_date=snapshot.maturity_date,
                            aci=snapshot.aci,
                            market_yield=snapshot.market_yield,
                            ai_comment="",
                        )
                    else:
                        snapshot = await moex_service.get_stock_snapshot(
                            item.ticker
                        )
                        current_price = snapshot.current_price
                        current_value = current_price * item.quantity
                        profit = (
                            (current_price - item.purchase_price)
                            * item.quantity
                        )
                        return InstrumentMetrics(
                            id=item.id,
                            type="stock",
                            name=snapshot.name,
                            ticker=snapshot.ticker,
                            current_price=round(current_price, 4),
                            purchase_price=item.purchase_price,
                            quantity=item.quantity,
                            current_value=round(current_value, 2),
                            profit=round(profit, 2),
                            weight=0.0,
                            company_rating=snapshot.company_rating,
                            dividend_yield=snapshot.dividend_yield,
                            ai_comment="",
                        )
                except Exception as exc:
                    itype = (
                        "bond"
                        if item.instrument_type == "bond"
                        else "stock"
                    )
                    loss = round(
                        -(item.purchase_price * item.quantity), 2
                    )
                    return InstrumentMetrics(
                        id=item.id,
                        type=itype,
                        name=item.ticker,
                        ticker=item.ticker,
                        current_price=0.0,
                        purchase_price=item.purchase_price,
                        quantity=item.quantity,
                        current_value=0.0,
                        profit=loss,
                        weight=0.0,
                        company_rating=None,
                        ai_comment=f"Нет рыночных данных: {str(exc)}",
                    )

        raw_rows = await asyncio.gather(
            *(fetch_row(item) for item in stored_items)
        )
        raw_rows = list(raw_rows)

        total_value = sum(item.current_value for item in raw_rows)
        finalized: list[InstrumentMetrics] = []

        for row in raw_rows:
            row.weight = (
                round((row.current_value / total_value) * 100, 2)
                if total_value > 0
                else 0.0
            )
            if not row.ai_comment:
                row.ai_comment = await llm_service.generate_comment(row)
            finalized.append(row)

        return finalized


portfolio_service = PortfolioService()
