import asyncio
from dataclasses import dataclass
import logging

from app.models import (
    AddInstrumentInput,
    InstrumentMetrics,
    UpdateCouponInput,
    UpdateCouponRateInput,
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
    manual_coupon_rate: float | None


class PortfolioService:
    async def validate(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        return await llm_service.validate_instrument(payload)

    async def add_instrument(
        self, portfolio_id: int, payload: AddInstrumentInput
    ) -> InstrumentMetrics:
        logger.info("add_instrument called: portfolio_id=%s ticker=%s quantity=%s purchase_price=%s",
                    portfolio_id, payload.ticker, payload.quantity, payload.purchase_price)
        validation = await self.validate(payload)
        if not validation.validated:
            warnings_msg = (
                "; ".join(validation.warnings)
                or "Ошибка валидации данных"
            )
            logger.warning(
                "Validation failed for %s: %s", payload.ticker, warnings_msg
            )
            raise ValidationError("Ошибка валидации", warnings_msg)

        ticker = payload.ticker.upper().strip()
        try:
            if validation.instrument_type == "bond":
                snapshot = await moex_service.get_bond_snapshot(ticker)
                if payload.purchase_price is None:
                    nominal = snapshot.nominal or 1000.0
                    purchase_price = round(
                        (snapshot.clean_price_percent / 100.0) * nominal
                        + (snapshot.aci or 0.0),
                        2,
                    )
                else:
                    purchase_price = payload.purchase_price
            else:
                snapshot = await moex_service.get_stock_snapshot(ticker)
                purchase_price = (
                    payload.purchase_price
                    if payload.purchase_price is not None
                    else snapshot.current_price
                )
        except Exception as exc:
            logger.exception("Failed to fetch market data for %s", ticker)
            raise

        new_id = storage_service.add_item(
            ticker=ticker,
            instrument_type=validation.instrument_type,
            quantity=payload.quantity,
            purchase_price=purchase_price,
            portfolio_id=portfolio_id,
        )
        logger.info("Added instrument %s (ID: %d) to portfolio_id=%d", ticker, new_id, portfolio_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh(portfolio_id)
        for row in rows:
            if row.id == new_id:
                return row

        logger.error(
            "Failed to retrieve added instrument %s from cache", ticker
        )
        raise ValueError(
            "Не удалось сформировать строку для добавленной бумаги"
        )

    async def add_instruments_bulk(
        self, portfolio_id: int, payloads: list[AddInstrumentInput]
    ) -> dict:
        """Add multiple instruments in parallel with retries, refresh cache once.

        Returns {"added": [...tickers], "failed": [...tickers]}.
        """
        MAX_RETRIES = 3
        RETRY_DELAYS = [1.0, 2.0]  # seconds before each retry

        async def _prepare_with_retry(payload: AddInstrumentInput) -> tuple:
            ticker = payload.ticker.upper().strip()
            last_exc: Exception | None = None
            for attempt in range(MAX_RETRIES):
                try:
                    validation = await self.validate(payload)
                    if not validation.validated:
                        raise ValidationError("Ошибка валидации", "; ".join(validation.warnings))
                    if validation.instrument_type == "bond":
                        snapshot = await moex_service.get_bond_snapshot(ticker)
                        if payload.purchase_price is None:
                            nominal = snapshot.nominal or 1000.0
                            price = round(
                                (snapshot.clean_price_percent / 100.0) * nominal + (snapshot.aci or 0.0), 2
                            )
                        else:
                            price = payload.purchase_price
                    else:
                        snapshot = await moex_service.get_stock_snapshot(ticker)
                        price = payload.purchase_price if payload.purchase_price is not None else snapshot.current_price
                    return ticker, validation.instrument_type, payload.quantity, price
                except ValidationError:
                    raise  # не ретраим ошибки валидации
                except Exception as exc:
                    last_exc = exc
                    if attempt < len(RETRY_DELAYS):
                        logger.warning("Bulk add attempt %d failed for %s: %s — retrying", attempt + 1, ticker, exc)
                        await asyncio.sleep(RETRY_DELAYS[attempt])
                    else:
                        logger.error("Bulk add failed after %d attempts for %s: %s", MAX_RETRIES, ticker, exc)
            raise last_exc  # type: ignore[misc]

        results = await asyncio.gather(
            *[_prepare_with_retry(p) for p in payloads], return_exceptions=True
        )

        added_tickers: list[str] = []
        failed_tickers: list[str] = []
        added_ids: list[int] = []

        for payload, res in zip(payloads, results):
            ticker = payload.ticker.upper().strip()
            if isinstance(res, Exception):
                failed_tickers.append(ticker)
                continue
            t, itype, quantity, price = res
            new_id = storage_service.add_item(
                ticker=t,
                instrument_type=itype,
                quantity=quantity,
                purchase_price=price,
                portfolio_id=portfolio_id,
            )
            added_ids.append(new_id)
            added_tickers.append(t)

        from app.services.cache_service import cache_service
        await cache_service.refresh(portfolio_id)
        return {"added": added_tickers, "failed": failed_tickers}

    def delete_instrument(self, portfolio_id: int, item_id: int) -> bool:
        deleted = storage_service.delete_item(item_id, portfolio_id)
        if deleted > 0:
            logger.info("Deleted instrument ID %d from portfolio_id=%d", item_id, portfolio_id)
            from app.services.cache_service import cache_service
            cache_service.invalidate(portfolio_id)
        return deleted > 0

    async def update_instrument(
        self,
        portfolio_id: int,
        item_id: int,
        payload: UpdateInstrumentInput,
    ) -> InstrumentMetrics:
        updated = storage_service.update_item(
            item_id=item_id,
            portfolio_id=portfolio_id,
            quantity=payload.quantity,
            purchase_price=payload.purchase_price,
        )
        if updated == 0:
            logger.warning(
                "Update failed: instrument ID %d not found", item_id
            )
            raise InstrumentNotFoundError(item_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh(portfolio_id)
        for row in rows:
            if row.id == item_id:
                logger.info("Updated instrument ID %d", item_id)
                return row

        logger.error("Failed to retrieve updated instrument ID %d", item_id)
        raise ValueError("Не удалось сформировать строку после обновления")

    async def update_coupon(
        self,
        portfolio_id: int,
        item_id: int,
        payload: UpdateCouponInput,
    ) -> InstrumentMetrics:
        updated = storage_service.update_coupon(
            item_id=item_id,
            portfolio_id=portfolio_id,
            coupon=payload.coupon,
        )
        if updated == 0:
            logger.warning(
                "Coupon update failed: instrument ID %d not found", item_id
            )
            raise InstrumentNotFoundError(item_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh(portfolio_id)
        for row in rows:
            if row.id == item_id:
                logger.info("Updated coupon for instrument ID %d", item_id)
                return row

        logger.error(
            "Failed to retrieve updated coupon for instrument ID %d", item_id
        )
        raise ValueError("Не удалось сформировать строку после обновления")

    async def update_coupon_rate(
        self,
        portfolio_id: int,
        item_id: int,
        payload: UpdateCouponRateInput,
    ) -> InstrumentMetrics:
        updated = storage_service.update_coupon_rate(
            item_id=item_id,
            portfolio_id=portfolio_id,
            coupon_rate=payload.coupon_rate,
        )
        if updated == 0:
            raise InstrumentNotFoundError(item_id)

        from app.services.cache_service import cache_service
        rows = await cache_service.refresh(portfolio_id)
        for row in rows:
            if row.id == item_id:
                return row

        raise ValueError("Не удалось сформировать строку после обновления")

    async def remove_not_found_instruments(self, portfolio_id: int) -> int:
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
                manual_coupon_rate=(
                    float(item["manual_coupon_rate"])
                    if item.get("manual_coupon_rate") is not None
                    else None
                ),
            )
            for item in storage_service.get_items(portfolio_id)
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
            deleted_count = storage_service.delete_items(missing_ids, portfolio_id)
            logger.info(
                "Removed %d not-found instruments: %s",
                deleted_count,
                missing_ids,
            )
            return deleted_count
        return 0

    async def get_table(self, portfolio_id: int) -> list[InstrumentMetrics]:
        """Return cached table (instant). Falls back to fresh fetch."""
        from app.services.cache_service import cache_service

        if cache_service.is_warm(portfolio_id):
            return cache_service.rows(portfolio_id)
        return await cache_service.refresh(portfolio_id)

    # ------------------------------------------------------------------
    # Heavy method: called ONLY by cache_service in background
    # ------------------------------------------------------------------
    async def get_table_fresh(self, portfolio_id: int) -> list[InstrumentMetrics]:
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
                manual_coupon_rate=(
                    float(item["manual_coupon_rate"])
                    if item.get("manual_coupon_rate") is not None
                    else None
                ),
            )
            for item in storage_service.get_items(portfolio_id)
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
                            nominal=nominal,
                            coupon=(
                                item.manual_coupon
                                if item.manual_coupon is not None
                                else snapshot.coupon
                            ),
                            coupon_period=snapshot.coupon_period,
                            coupon_rate=(
                                item.manual_coupon_rate
                                if item.manual_coupon_rate is not None
                                else snapshot.coupon_rate
                            ),
                            manual_coupon_set=item.manual_coupon is not None,
                            manual_coupon_rate_set=item.manual_coupon_rate is not None,
                            maturity_date=snapshot.maturity_date,
                            buyback_date=snapshot.buyback_date,
                            offer_date=snapshot.offer_date,
                            next_coupon_date=snapshot.next_coupon_date,
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
                        coupon=item.manual_coupon,
                        coupon_rate=item.manual_coupon_rate,
                        manual_coupon_set=item.manual_coupon is not None,
                        manual_coupon_rate_set=item.manual_coupon_rate is not None,
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

            # Persist rating to DB so we can detect changes across restarts
            if row.current_price > 0:
                storage_service.update_rating(row.id, portfolio_id, row.company_rating)

        return finalized


portfolio_service = PortfolioService()
