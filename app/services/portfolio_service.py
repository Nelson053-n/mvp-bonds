import asyncio
from dataclasses import dataclass
from datetime import date
import logging
from typing import TYPE_CHECKING

from app.models import (
    AddInstrumentInput,
    InstrumentMetrics,
    UpdateCouponInput,
    UpdateCouponRateInput,
    UpdateInstrumentInput,
    ValidationResponse,
)
from app.services.coupon_schedule_service import coupon_schedule_service
from app.services.llm_service import llm_service
from app.services.moex_service import moex_service
from app.services.storage_service import storage_service
from app.exceptions import ValidationError, InstrumentNotFoundError

if TYPE_CHECKING:
    from app.services.cache_service import CacheService

logger = logging.getLogger(__name__)


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _get_cache() -> "CacheService":
    """Lazy import to break circular dependency with cache_service."""
    from app.services.cache_service import cache_service
    return cache_service


@dataclass
class PortfolioItem:
    id: int
    ticker: str
    instrument_type: str
    quantity: float
    purchase_price: float
    manual_coupon: float | None
    manual_coupon_rate: float | None
    figi: str | None = None
    purchase_date: str | None = None  # ISO date or None
    source: str | None = None         # 'manual' | 'tbank' | 'custom'
    custom_name: str | None = None    # off-exchange item name
    custom_price: float | None = None # off-exchange current price
    custom_nominal: float | None = None       # off-exchange bond nominal
    custom_coupon_freq: int | None = None      # coupon payments per year (2/4/12)
    custom_maturity: str | None = None         # maturity date ISO (optional)
    company_rating: str | None = None          # auto rating from last market snapshot
    manual_rating: str | None = None           # user-set rating; overrides auto

    @classmethod
    def from_dict(cls, item: dict) -> "PortfolioItem":
        return cls(
            id=int(item["id"]),
            ticker=str(item["ticker"]),
            instrument_type=str(item["instrument_type"]),
            quantity=float(item["quantity"]),
            purchase_price=float(item["purchase_price"]),
            manual_coupon=(
                float(item["manual_coupon"]) if item["manual_coupon"] is not None else None
            ),
            manual_coupon_rate=(
                float(item["manual_coupon_rate"]) if item.get("manual_coupon_rate") is not None else None
            ),
            figi=item.get("figi"),
            purchase_date=item.get("purchase_date"),
            source=item.get("source"),
            custom_name=item.get("custom_name"),
            custom_price=item.get("custom_price"),
            custom_nominal=item.get("custom_nominal"),
            custom_coupon_freq=item.get("custom_coupon_freq"),
            custom_maturity=item.get("custom_maturity"),
            company_rating=item.get("company_rating"),
            manual_rating=item.get("manual_rating"),
        )


class PortfolioService:
    async def validate(
        self, payload: AddInstrumentInput
    ) -> ValidationResponse:
        return await llm_service.validate_instrument(payload)

    async def _fetch_snapshot_and_price(
        self, ticker: str, instrument_type: str, override_price: float | None
    ) -> tuple[object, float]:
        """Fetch MOEX snapshot and compute purchase price. Returns (snapshot, price)."""
        if instrument_type == "bond":
            snapshot = await moex_service.get_bond_snapshot(ticker)
            if override_price is None:
                nominal = snapshot.nominal or 1000.0
                clean_price = (snapshot.clean_price_percent / 100.0) * nominal
                # Сохраняем чистую цену (без НКД) — как на бирже
                price = round(clean_price, 2)
            else:
                price = override_price
        else:
            snapshot = await moex_service.get_stock_snapshot(ticker)
            price = override_price if override_price is not None else snapshot.current_price
        return snapshot, price

    async def add_instrument(
        self, portfolio_id: int, payload: AddInstrumentInput
    ) -> InstrumentMetrics:
        logger.info("add_instrument called: portfolio_id=%s ticker=%s quantity=%s purchase_price=%s",
                    portfolio_id, payload.ticker, payload.quantity, payload.purchase_price)

        # Off-exchange item: skip validation and MOEX entirely. The user supplies
        # everything (name, type, prices, coupon). ticker is just a label.
        if payload.is_custom:
            if payload.purchase_price is None:
                raise ValidationError("Ошибка валидации", "Укажите цену покупки")
            itype = payload.instrument_type or "bond"
            new_id = storage_service.add_item(
                ticker=payload.ticker.upper().strip(),
                instrument_type=itype,
                quantity=payload.quantity,
                purchase_price=payload.purchase_price,
                portfolio_id=portfolio_id,
                source="custom",
                purchase_date=payload.purchase_date.isoformat() if payload.purchase_date else None,
                custom_name=payload.custom_name,
                custom_price=payload.current_price if payload.current_price is not None else payload.purchase_price,
                custom_nominal=payload.custom_nominal,
                custom_coupon_freq=payload.custom_coupon_freq,
                custom_maturity=payload.custom_maturity.isoformat() if payload.custom_maturity else None,
            )
            if payload.coupon_rate is not None:
                storage_service.update_coupon_rate(new_id, portfolio_id, payload.coupon_rate)
            rows = await _get_cache().refresh(portfolio_id)
            for row in rows:
                if row.id == new_id:
                    return row
            raise ValueError("Не удалось сформировать строку для добавленной бумаги")

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
            _, purchase_price = await self._fetch_snapshot_and_price(
                ticker, validation.instrument_type, payload.purchase_price
            )
        except Exception:
            logger.exception("Failed to fetch market data for %s", ticker)
            raise

        new_id = storage_service.add_item(
            ticker=ticker,
            instrument_type=validation.instrument_type,
            quantity=payload.quantity,
            purchase_price=purchase_price,
            portfolio_id=portfolio_id,
            purchase_date=payload.purchase_date.isoformat() if payload.purchase_date else None,
        )
        logger.info("Added instrument %s (ID: %d) to portfolio_id=%d", ticker, new_id, portfolio_id)

        rows = await _get_cache().refresh(portfolio_id)
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
                    _, price = await self._fetch_snapshot_and_price(
                        ticker, validation.instrument_type, payload.purchase_price
                    )
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

        await _get_cache().refresh(portfolio_id)
        return {"added": added_tickers, "failed": failed_tickers}

    def delete_instrument(self, portfolio_id: int, item_id: int) -> bool:
        deleted = storage_service.delete_item(item_id, portfolio_id)
        if deleted > 0:
            logger.info("Deleted instrument ID %d from portfolio_id=%d", item_id, portfolio_id)
            _get_cache().invalidate(portfolio_id)
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
            purchase_date=payload.purchase_date.isoformat() if payload.purchase_date else None,
            custom_price=payload.current_price,
            custom_nominal=payload.custom_nominal,
            custom_coupon_freq=payload.custom_coupon_freq,
            custom_maturity=payload.custom_maturity.isoformat() if payload.custom_maturity else None,
        )
        if updated == 0:
            logger.warning(
                "Update failed: instrument ID %d not found", item_id
            )
            raise InstrumentNotFoundError(item_id)

        if "manual_rating" in payload.model_fields_set:
            rating = (payload.manual_rating or "").strip() or None
            storage_service.update_manual_rating(
                item_id=item_id, portfolio_id=portfolio_id, rating=rating
            )

        if "coupon_rate" in payload.model_fields_set and payload.coupon_rate is not None:
            storage_service.update_coupon_rate(
                item_id=item_id, portfolio_id=portfolio_id, coupon_rate=payload.coupon_rate
            )

        rows = await _get_cache().refresh(portfolio_id)
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

        rows = await _get_cache().refresh(portfolio_id)
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

        rows = await _get_cache().refresh(portfolio_id)
        for row in rows:
            if row.id == item_id:
                return row

        raise ValueError("Не удалось сформировать строку после обновления")

    async def remove_not_found_instruments(self, portfolio_id: int) -> int:
        stored_items = [
            PortfolioItem.from_dict(item)
            for item in storage_service.get_items(portfolio_id)
        ]

        async def _check_exists(item: PortfolioItem) -> int | None:
            """Return item.id if not found on MOEX, else None."""
            try:
                if item.instrument_type == "bond":
                    await moex_service.get_bond_snapshot(item.ticker)
                else:
                    await moex_service.get_stock_snapshot(item.ticker)
                return None
            except Exception as exc:
                logger.warning(
                    "Instrument %s (%s) not found on MOEX: %s",
                    item.ticker, item.instrument_type, exc,
                )
                return item.id

        results = await asyncio.gather(*(_check_exists(item) for item in stored_items))
        missing_ids = [item_id for item_id in results if item_id is not None]

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

    async def get_cash_rub(self, portfolio_id: int) -> float:
        """Free cash of a portfolio converted to RUB (0 if none / not synced).

        Portfolio value = securities + cash, so this is added to snapshots: selling
        a bond into cash must not drop the total value.
        """
        import json as _json
        cfg = storage_service.get_sync_config(portfolio_id)
        if not cfg or not cfg.get("cash_balance"):
            return 0.0
        try:
            entries = _json.loads(cfg["cash_balance"])
        except (ValueError, TypeError):
            return 0.0
        total = 0.0
        for c in entries:
            ccy = (c.get("currency") or "").upper()
            amt = float(c.get("amount") or 0)
            if ccy in ("RUB", "SUR", ""):
                total += amt
            else:
                rate = await moex_service._get_fx_rate(ccy)
                if rate:
                    total += amt * rate
        return total

    # ------------------------------------------------------------------
    # Heavy method: called ONLY by cache_service in background
    # ------------------------------------------------------------------
    async def get_table_fresh(self, portfolio_id: int) -> list[InstrumentMetrics]:
        stored_items = [
            PortfolioItem.from_dict(item)
            for item in storage_service.get_items(portfolio_id)
        ]

        # Realized coupons per figi (T-Bank synced portfolios only; empty otherwise)
        coupons_map = storage_service.get_tbank_coupons(portfolio_id)

        # Limit concurrent MOEX requests to avoid rate-limits/timeouts
        semaphore = asyncio.Semaphore(8)

        async def fetch_row(item: PortfolioItem) -> InstrumentMetrics:
            async with semaphore:
                # Off-exchange (custom) item: no MOEX lookup. Build the row from the
                # user-entered name / current price / coupon. current_price falls
                # back to purchase_price (profit 0) until the user edits it.
                if item.source == "custom":
                    cur = item.custom_price if item.custom_price is not None else item.purchase_price
                    profit = (cur - item.purchase_price) * item.quantity
                    # Coupon cash-flow fields for the «Денежный поток» widget.
                    # The user supplies an annual coupon rate; we derive the per-period
                    # payment amount, the period in days, and an anchor next-coupon date.
                    nominal = item.custom_nominal if item.custom_nominal is not None else 1000.0
                    freq = item.custom_coupon_freq if item.custom_coupon_freq else 2
                    coupon_period = 365 // freq
                    coupon_amount: float | None = None
                    next_coupon_date: date | None = None
                    if item.manual_coupon_rate is not None and item.manual_coupon_rate > 0:
                        coupon_amount = round(nominal * (item.manual_coupon_rate / 100.0) / freq, 4)
                        # Anchor: nearest future payment date stepping forward from the
                        # purchase date (or today) with a coupon_period-day stride.
                        anchor = None
                        if item.purchase_date:
                            try:
                                anchor = date.fromisoformat(str(item.purchase_date)[:10])
                            except ValueError:
                                anchor = None
                        if anchor is None:
                            anchor = date.today()
                        today = date.today()
                        from datetime import timedelta
                        step = timedelta(days=coupon_period)
                        d = anchor
                        while d < today:
                            d = d + step
                        next_coupon_date = d
                    maturity_date: date | None = None
                    if item.custom_maturity:
                        try:
                            maturity_date = date.fromisoformat(str(item.custom_maturity)[:10])
                        except ValueError:
                            maturity_date = None
                    # Full profit = price revaluation + coupons received since purchase.
                    # No ACI for custom items (the user doesn't enter it). Coupons are
                    # counted from the purchase date stepping by coupon_period days up to
                    # today (and never past the maturity date).
                    realized = 0.0
                    has_realized = False
                    if (
                        coupon_amount is not None
                        and item.purchase_date
                        and item.instrument_type == "bond"
                    ):
                        buy = _parse_iso_date(item.purchase_date)
                        if buy is not None:
                            from datetime import timedelta
                            today = date.today()
                            end = today
                            if maturity_date is not None and maturity_date < end:
                                end = maturity_date
                            paid = 0
                            d = buy + timedelta(days=coupon_period)
                            while d <= end:
                                paid += 1
                                d = d + timedelta(days=coupon_period)
                            realized = coupon_amount * item.quantity * paid
                            has_realized = True
                    full_profit_val = (
                        round(profit + realized, 2) if has_realized else None
                    )
                    return InstrumentMetrics(
                        id=item.id,
                        type=item.instrument_type,
                        name=item.custom_name or item.ticker,
                        ticker=item.ticker,
                        current_price=round(cur, 4),
                        purchase_price=item.purchase_price,
                        quantity=item.quantity,
                        current_value=round(cur * item.quantity, 2),
                        profit=round(profit, 2),
                        weight=0.0,
                        is_traded=False,
                        coupon=coupon_amount if coupon_amount is not None else item.manual_coupon,
                        coupon_period=coupon_period if coupon_amount is not None else None,
                        coupon_rate=item.manual_coupon_rate,
                        manual_coupon_set=item.manual_coupon is not None,
                        manual_coupon_rate_set=item.manual_coupon_rate is not None,
                        next_coupon_date=next_coupon_date,
                        maturity_date=maturity_date,
                        nominal=nominal if item.instrument_type == "bond" else None,
                        company_rating=item.manual_rating,
                        realized_coupons=round(realized, 2) if has_realized else None,
                        full_profit=full_profit_val,
                        purchase_date=item.purchase_date,
                        source="custom",
                        ai_comment="",
                    )
                try:
                    if item.instrument_type == "bond":
                        snapshot = await moex_service.get_bond_snapshot(
                            item.ticker
                        )
                        nominal = snapshot.nominal or 1000.0
                        clean_price = (
                            (snapshot.clean_price_percent / 100.0) * nominal
                        )
                        dirty_price = clean_price + (snapshot.aci or 0.0)
                        # current_price в таблице — чистая цена (как на бирже)
                        current_price = clean_price
                        # стоимость позиции считается по грязной цене
                        current_value = dirty_price * item.quantity
                        profit = (
                            (clean_price - item.purchase_price)
                            * item.quantity
                        )
                        # Day P&L: change in clean price vs previous session close × qty
                        day_profit_val: float | None = None
                        prev_close_value: float | None = None
                        if snapshot.prev_close_percent is not None:
                            prev_clean = (snapshot.prev_close_percent / 100.0) * nominal
                            day_profit_val = (clean_price - prev_clean) * item.quantity
                            prev_close_value = (prev_clean + (snapshot.aci or 0.0)) * item.quantity
                        # For floaters a live exchange coupon always wins over a
                        # stale manual entry; manual stays only as fallback.
                        floater_coupon_known = (
                            snapshot.is_floater
                            and snapshot.coupon is not None
                            and snapshot.coupon > 0
                        )
                        floater_rate_known = (
                            snapshot.is_floater
                            and snapshot.coupon_rate is not None
                            and snapshot.coupon_rate > 0
                        )
                        # Full profit = revaluation + current ACI + realized coupons.
                        # Two sources of realized coupons:
                        #  • T-Bank synced positions: exact figures from the operations
                        #    journal, stored per figi (already summed, no ×quantity).
                        #  • Manually-added bonds with a purchase_date: coupons paid since
                        #    that date, from the MOEX coupon schedule (per-bond value, so
                        #    ×quantity, and ×fx_rate for non-RUB bonds).
                        tbank_synced = item.figi in coupons_map
                        if tbank_synced:
                            realized = coupons_map.get(item.figi, {}).get("coupons_total", 0.0)
                            has_realized = True
                        elif item.purchase_date:
                            since = _parse_iso_date(item.purchase_date)
                            per_bond = (
                                await coupon_schedule_service.realized_coupons_per_bond(
                                    item.ticker, since
                                )
                                if since else 0.0
                            )
                            realized = per_bond * item.quantity * (snapshot.fx_rate or 1.0)
                            has_realized = True
                        else:
                            realized = 0.0
                            has_realized = False
                        full_profit_val = (
                            profit + (snapshot.aci or 0.0) * item.quantity + realized
                            if has_realized else None
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
                            day_profit=round(day_profit_val, 2) if day_profit_val is not None else None,
                            prev_close_value=round(prev_close_value, 2) if prev_close_value is not None else None,
                            weight=0.0,
                            company_rating=item.manual_rating or snapshot.company_rating or item.company_rating,
                            is_qual=snapshot.is_qual,
                            is_traded=snapshot.is_traded,
                            nominal=nominal,
                            coupon=(
                                snapshot.coupon
                                if floater_coupon_known
                                else (item.manual_coupon if item.manual_coupon is not None else snapshot.coupon)
                            ),
                            coupon_period=snapshot.coupon_period,
                            coupon_rate=(
                                snapshot.coupon_rate
                                if floater_rate_known
                                else (item.manual_coupon_rate if item.manual_coupon_rate is not None else snapshot.coupon_rate)
                            ),
                            manual_coupon_set=(
                                False if floater_coupon_known else item.manual_coupon is not None
                            ),
                            manual_coupon_rate_set=(
                                False if floater_rate_known else item.manual_coupon_rate is not None
                            ),
                            is_floater=snapshot.is_floater,
                            maturity_date=snapshot.maturity_date,
                            buyback_date=snapshot.buyback_date,
                            offer_date=snapshot.offer_date,
                            next_coupon_date=snapshot.next_coupon_date,
                            aci=snapshot.aci,
                            realized_coupons=round(realized, 2) if has_realized else None,
                            full_profit=round(full_profit_val, 2) if full_profit_val is not None else None,
                            market_yield=snapshot.market_yield,
                            face_unit=snapshot.face_unit,
                            fx_rate=snapshot.fx_rate,
                            purchase_date=item.purchase_date,
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
                        day_profit_val: float | None = None
                        prev_close_value: float | None = None
                        if snapshot.prev_close_price is not None:
                            day_profit_val = (current_price - snapshot.prev_close_price) * item.quantity
                            prev_close_value = snapshot.prev_close_price * item.quantity
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
                            day_profit=round(day_profit_val, 2) if day_profit_val is not None else None,
                            prev_close_value=round(prev_close_value, 2) if prev_close_value is not None else None,
                            weight=0.0,
                            company_rating=item.manual_rating or snapshot.company_rating or item.company_rating,
                            dividend_yield=snapshot.dividend_yield,
                            purchase_date=item.purchase_date,
                            ai_comment="",
                        )
                except Exception as exc:
                    logger.warning(
                        "Metrics calc failed for %s (id=%s, type=%s) in portfolio_id=%s: %s: %s",
                        item.ticker,
                        item.id,
                        item.instrument_type,
                        portfolio_id,
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
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
                        purchase_date=item.purchase_date,
                        ai_comment=f"Нет рыночных данных: {str(exc)}",
                    )

        raw_rows = list(await asyncio.gather(
            *(fetch_row(item) for item in stored_items)
        ))

        total_value = sum(item.current_value for item in raw_rows)
        for row in raw_rows:
            row.weight = (
                round((row.current_value / total_value) * 100, 2)
                if total_value > 0
                else 0.0
            )

        # Generate AI comments in parallel
        async def _gen_comment(row: InstrumentMetrics) -> InstrumentMetrics:
            if not row.ai_comment:
                row.ai_comment = await llm_service.generate_comment(row)
            return row

        finalized: list[InstrumentMetrics] = list(
            await asyncio.gather(*(_gen_comment(row) for row in raw_rows))
        )

        # Persist ratings + coupon_rate to DB for risk calculation and restarts
        for row in finalized:
            if row.current_price > 0:
                storage_service.update_snapshot_data(
                    row.id, portfolio_id, row.company_rating, row.coupon_rate
                )

        return finalized


portfolio_service = PortfolioService()
