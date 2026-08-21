"""T-Bank Invest API integration for portfolio import and sync.

Uses T-Bank REST API (no SDK). Token is passed per-request or decrypted from DB.
"""

import asyncio
import logging
import ssl
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import certifi
import httpx

if TYPE_CHECKING:
    from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)

# С 07.08.2026 invest-public-api.tinkoff.ru отдаёт сертификат, выпущенный
# «Russian Trusted Root CA» (Минцифры). Этого корня нет ни в certifi, ни в
# системном хранилище Ubuntu, поэтому проверка падала с
# CERTIFICATE_VERIFY_FAILED и синк не работал вовсе.
#
# Доверие расширяется ТОЛЬКО для клиента T-Bank: контекст берёт обычные
# корни certifi ПЛЮС корень Минцифры. Остальные исходящие запросы
# (MOEX, ЮKassa, Telegram) продолжают использовать certifi без изменений.
# Проверка сертификата не отключается — verify=False недопустим.
_RU_ROOT_CA = Path(__file__).resolve().parents[2] / "certs" / "russian_trusted_root_ca.pem"


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """SSL context trusting the usual CAs plus the Russian Trusted Root CA."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if _RU_ROOT_CA.is_file():
        try:
            ctx.load_verify_locations(cafile=str(_RU_ROOT_CA))
        except ssl.SSLError:
            logger.exception("Не удалось загрузить корневой сертификат %s", _RU_ROOT_CA)
    else:
        logger.error(
            "Корневой сертификат %s не найден — TLS к T-Bank API работать не будет",
            _RU_ROOT_CA,
        )
    return ctx


_BASE = "https://invest-public-api.tinkoff.ru/rest"
_ACCOUNTS_URL = f"{_BASE}/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
_PORTFOLIO_URL = f"{_BASE}/tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio"
_OPERATIONS_URL = f"{_BASE}/tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations"

# Retries for transient network failures. Kept short: the sync loop runs every
# 10 minutes, so a long backoff would just collide with the next cycle.
_RETRY_ATTEMPTS = 3
_RETRY_DELAYS = (1.0, 3.0)  # pauses between attempts 1→2 and 2→3

# T-Bank REST API returns lowercase instrument types
_TYPE_MAP = {
    "bond": "bond",
    "share": "stock",
}


class TBankError(Exception):
    """T-Bank API error with a user-facing message."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _money_value(mv: dict | None) -> float:
    if not mv:
        return 0.0
    return int(mv.get("units") or 0) + int(mv.get("nano") or 0) / 1_000_000_000


def _quotation(q: dict | None) -> float:
    if not q:
        return 0.0
    return int(q.get("units") or 0) + int(q.get("nano") or 0) / 1_000_000_000


_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Один HTTP-клиент на процесс для всех синков T-Bank.

    TBankService создаётся на каждый синк (per-request), поэтому клиент
    нельзя держать в экземпляре — он общий на уровне модуля. Раньше на
    каждый POST открывался свой AsyncClient: пул соединений и TLS-сессия
    строились заново, а память после закрытия не возвращалась ОС
    (замерено: ~700 КБ на SSL-контекст). Сам контекст уже кэширован
    через lru_cache, теперь переиспользуется и соединение.
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            verify=_ssl_context(),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
    return _shared_client


async def aclose_client() -> None:
    """Закрыть общий клиент (вызывается на shutdown приложения)."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


class TBankService:
    """Per-request service for reading T-Bank portfolio data."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _post(self, url: str, payload: dict, timeout: float = 15) -> httpx.Response:
        """POST to the T-Bank API, retrying transient network failures.

        A single ConnectTimeout used to fail the whole sync and leave the
        portfolio stale for a full 10-minute cycle (observed on prod
        2026-08-08 04:41). Only connection-level errors are retried: HTTP
        errors surface as TBankError from _check_response and must NOT be
        repeated — a 401 disables sync on purpose, and hammering a 429
        rate-limit makes it worse.
        """
        last_exc: httpx.RequestError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                client = _get_client()
                return await client.post(
                    url, json=payload, headers=self._headers, timeout=timeout
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt + 1 < _RETRY_ATTEMPTS:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        "T-Bank %s: %s (попытка %d/%d), повтор через %.1fс",
                        url.rsplit("/", 1)[-1], type(exc).__name__,
                        attempt + 1, _RETRY_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay)
        raise TBankError(
            f"Т-Банк API недоступен после {_RETRY_ATTEMPTS} попыток: {last_exc}"
        )

    def _check_response(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise TBankError("Неверный токен Т-Банка")
        if resp.status_code == 429:
            raise TBankError("Лимит запросов к Т-Банк API превышен, попробуйте позже")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:
                pass
            raise TBankError(f"Ошибка Т-Банк API: {resp.status_code} {detail}".strip())

    async def get_accounts(self) -> list[dict]:
        """Return [{id, name, type}] for the token."""
        resp = await self._post(_ACCOUNTS_URL, {})
        self._check_response(resp)
        return [
            {"id": a["id"], "name": a.get("name", ""), "type": a.get("type", "")}
            for a in resp.json().get("accounts", [])
        ]

    async def get_positions(self, account_id: str) -> list[dict]:
        """Return raw positions list from GetPortfolio."""
        resp = await self._post(_PORTFOLIO_URL, {"accountId": account_id})
        self._check_response(resp)
        return resp.json().get("positions", [])

    async def get_portfolio_full(self, account_id: str) -> tuple[list[dict], list[dict]]:
        """Return (positions, cash) from GetPortfolio.

        cash is a list of {currency, amount} extracted from positions with
        instrumentType=='currency'. Currency code is parsed from ticker prefix
        (T-Bank uses RUB000UTSTOM / USD000UTSTOM / EUR_RUB__TOM / CNYRUB_TOM …).
        """
        resp = await self._post(_PORTFOLIO_URL, {"accountId": account_id})
        self._check_response(resp)
        data = resp.json()
        positions = data.get("positions", [])
        cash: list[dict] = []
        for pos in positions:
            if pos.get("instrumentType") != "currency":
                continue
            amount = _quotation(pos.get("quantity"))
            if amount == 0:
                continue
            ticker = (pos.get("ticker") or "").upper()
            currency = ticker[:3] if len(ticker) >= 3 else ""
            if not currency:
                continue
            cash.append({"currency": currency, "amount": round(amount, 2)})
        return positions, cash

    async def get_operations(self, account_id: str, from_date: str, to_date: str) -> list[dict]:
        """Return executed operations in [from_date, to_date] (RFC3339).

        Each item: {figi, date, operation_type, payment}. payment is signed RUB
        (MoneyValue units/nano); coupons come back positive.
        """
        resp = await self._post(
            _OPERATIONS_URL,
            {
                "accountId": account_id,
                "from": from_date,
                "to": to_date,
                "state": "OPERATION_STATE_EXECUTED",
            },
            timeout=30,
        )
        self._check_response(resp)
        out: list[dict] = []
        for op in resp.json().get("operations", []):
            out.append({
                "figi": op.get("figi") or "",
                "date": op.get("date") or "",
                "operation_type": op.get("operationType") or "",
                "payment": _money_value(op.get("payment")),
            })
        return out

    @staticmethod
    def _operations_to_coupons_and_buys(operations: list[dict]) -> dict[str, dict]:
        """Aggregate operations by figi → {coupons, first_buy, has_sell, last_sell}.

        has_sell/last_sell let the sync tell an actual sale (position legitimately
        gone) apart from a position that merely vanished from a flaky API response.
        """
        agg: dict[str, dict] = {}
        for op in operations:
            figi = op.get("figi")
            if not figi:
                continue
            entry = agg.setdefault(
                figi, {"coupons": 0.0, "first_buy": None, "has_sell": False, "last_sell": None}
            )
            otype = op.get("operation_type", "")
            if otype == "OPERATION_TYPE_COUPON":
                entry["coupons"] += float(op.get("payment") or 0.0)
            elif otype in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_CARD"):
                d = op.get("date") or ""
                if d and (entry["first_buy"] is None or d < entry["first_buy"]):
                    entry["first_buy"] = d
            elif otype in ("OPERATION_TYPE_SELL", "OPERATION_TYPE_SELL_CARD"):
                entry["has_sell"] = True
                d = op.get("date") or ""
                if d and (entry["last_sell"] is None or d > entry["last_sell"]):
                    entry["last_sell"] = d
        for entry in agg.values():
            entry["coupons"] = round(entry["coupons"], 2)
        return agg

    @staticmethod
    def _positions_to_items(positions: list[dict]) -> list[dict]:
        """Convert raw T-Bank positions into importable items."""
        items: list[dict] = []
        for pos in positions:
            instrument_type = _TYPE_MAP.get(pos.get("instrumentType", ""))
            if instrument_type is None:
                continue

            quantity = _quotation(pos.get("quantity"))
            if quantity <= 0:
                continue

            purchase_price = _money_value(pos.get("averagePositionPrice"))
            if purchase_price <= 0:
                purchase_price = _money_value(pos.get("currentPrice"))
            if purchase_price <= 0:
                logger.warning("No price for figi=%s, skipping", pos.get("figi"))
                continue

            ticker = pos.get("ticker", "")
            if not ticker:
                logger.warning("No ticker for figi=%s, skipping", pos.get("figi"))
                continue

            items.append({
                "ticker": ticker.upper(),
                "instrument_type": instrument_type,
                "quantity": quantity,
                "purchase_price": round(purchase_price, 2),
                "figi": pos.get("figi") or None,
            })
        return items

    async def import_account(self, account_id: str) -> list[dict]:
        """Fetch positions and return importable items.

        Returns list of {ticker, instrument_type, quantity, purchase_price}.
        Skips unsupported types and zero-quantity positions.
        Ticker is taken directly from the position data (T-Bank REST API includes it).
        """
        positions = await self.get_positions(account_id)
        return self._positions_to_items(positions)

    async def sync_portfolio(
        self,
        portfolio_id: int,
        account_id: str,
        bonds_only: bool,
        storage: "StorageService",
    ) -> dict:
        """Sync T-Bank positions into an existing portfolio.

        Returns {added, updated, removed_candidates, errors, cash}.
        New/changed positions are written to DB. Disappeared positions are
        returned as removed_candidates — NOT auto-deleted.
        Cash balance (list of {currency, amount}) is persisted on the sync row.
        """
        import json

        from app.services.cache_service import cache_service

        # Fetch live positions + cash from broker
        try:
            positions, cash = await self.get_portfolio_full(account_id)
            api_items = self._positions_to_items(positions)
        except TBankError:
            raise
        except Exception as exc:
            raise TBankError(f"Ошибка получения позиций: {exc}") from exc

        # Persist cash snapshot (always, even if empty)
        try:
            storage.update_sync_cash(portfolio_id, json.dumps(cash, ensure_ascii=False))
        except Exception as exc:
            logger.warning("update_sync_cash failed for portfolio_id=%d: %s", portfolio_id, exc)

        if bonds_only:
            api_items = [i for i in api_items if i["instrument_type"] == "bond"]

        # Index API positions by (ticker, instrument_type)
        api_map: dict[tuple[str, str], dict] = {
            (i["ticker"], i["instrument_type"]): i for i in api_items
        }

        # Current T-Bank positions in DB
        db_items = storage.get_tbank_items(portfolio_id)
        db_map: dict[tuple[str, str], dict] = {
            (i["ticker"], i["instrument_type"]): i for i in db_items
        }

        added = 0
        updated = 0
        errors: list[str] = []
        removed_candidates: list[str] = []

        # Process API positions
        for key, api_item in api_map.items():
            db_item = db_map.get(key)
            try:
                if db_item is None:
                    # New position
                    storage.add_item(
                        ticker=api_item["ticker"],
                        instrument_type=api_item["instrument_type"],
                        quantity=api_item["quantity"],
                        purchase_price=api_item["purchase_price"],
                        portfolio_id=portfolio_id,
                        source="tbank",
                        figi=api_item.get("figi"),
                    )
                    added += 1
                elif (
                    abs(db_item["quantity"] - api_item["quantity"]) > 1e-6
                    or abs(db_item["purchase_price"] - api_item["purchase_price"]) > 0.01
                    or (not db_item.get("figi") and api_item.get("figi"))
                ):
                    # Quantity / price changed, or figi not yet stored — sync from API
                    storage.update_item(
                        item_id=db_item["id"],
                        portfolio_id=portfolio_id,
                        quantity=api_item["quantity"],
                        purchase_price=api_item["purchase_price"],
                        figi=api_item.get("figi"),
                    )
                    updated += 1
            except Exception as exc:
                logger.warning("sync_portfolio: error processing %s: %s", key, exc)
                errors.append(api_item["ticker"])

        # Realized coupons + sell facts from the operations journal. Fetched BEFORE
        # handling disappeared positions so we can tell a real sale apart from a
        # position that merely fell out of a flaky GetPortfolio response.
        # (best-effort: never fail the sync on a journal error)
        agg: dict[str, dict] = {}
        try:
            from datetime import datetime, timezone
            now_dt = datetime.now(timezone.utc)
            # Always re-fetch the full window: coupon totals are absolute sums per figi,
            # and the window is bounded (T-Bank keeps ~3 years of operations).
            from_date = now_dt.replace(year=now_dt.year - 5).isoformat()
            to_date = now_dt.isoformat()
            operations = await self.get_operations(account_id, from_date, to_date)
            agg = self._operations_to_coupons_and_buys(operations)
            for figi, data in agg.items():
                storage.upsert_tbank_coupons(
                    portfolio_id, figi, data["coupons"], data.get("first_buy")
                )
            storage.update_operations_sync_at(portfolio_id, to_date)
            cache_service.invalidate(portfolio_id)
        except Exception as exc:
            logger.warning(
                "sync_portfolio: operations journal fetch failed for portfolio_id=%d: %s",
                portfolio_id, exc,
            )

        # Positions present in DB but gone from broker. If the journal confirms a
        # SELL for that figi, the position was genuinely closed → auto-remove it
        # (and drop its coupon record) so it stops counting as phantom profit.
        # Without a confirmed sale we keep the old conservative behaviour: report
        # it as a removal candidate for the user to confirm.
        removed = 0
        for key, db_item in db_map.items():
            if key in api_map:
                continue
            figi = db_item.get("figi")
            sold = bool(figi and agg.get(figi, {}).get("has_sell"))
            if sold:
                try:
                    storage.delete_item(db_item["id"], portfolio_id)
                    storage.delete_tbank_coupons(portfolio_id, figi)
                    removed += 1
                    logger.info(
                        "AUDIT tbank_sync auto-removed sold position: portfolio_id=%d ticker=%s figi=%s",
                        portfolio_id, db_item["ticker"], figi,
                    )
                except Exception as exc:
                    logger.warning("sync_portfolio: failed to remove sold %s: %s", db_item["ticker"], exc)
                    removed_candidates.append(db_item["ticker"])
            else:
                removed_candidates.append(db_item["ticker"])

        if added > 0 or updated > 0 or removed > 0:
            cache_service.invalidate(portfolio_id)

        logger.info(
            "AUDIT tbank_sync: portfolio_id=%d added=%d updated=%d removed=%d removed_candidates=%d errors=%d",
            portfolio_id, added, updated, removed, len(removed_candidates), len(errors),
        )

        return {
            "added": added,
            "updated": updated,
            "removed": removed,
            "removed_candidates": removed_candidates,
            "errors": errors,
            "cash": cash,
        }
