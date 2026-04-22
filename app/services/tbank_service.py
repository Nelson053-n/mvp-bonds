"""T-Bank Invest API integration for portfolio import and sync.

Uses T-Bank REST API (no SDK). Token is passed per-request or decrypted from DB.
"""

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)

_BASE = "https://invest-public-api.tinkoff.ru/rest"
_ACCOUNTS_URL = f"{_BASE}/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
_PORTFOLIO_URL = f"{_BASE}/tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio"

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


class TBankService:
    """Per-request service for reading T-Bank portfolio data."""

    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_ACCOUNTS_URL, json={}, headers=self._headers)
        self._check_response(resp)
        return [
            {"id": a["id"], "name": a.get("name", ""), "type": a.get("type", "")}
            for a in resp.json().get("accounts", [])
        ]

    async def get_positions(self, account_id: str) -> list[dict]:
        """Return raw positions list from GetPortfolio."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _PORTFOLIO_URL,
                json={"accountId": account_id},
                headers=self._headers,
            )
        self._check_response(resp)
        return resp.json().get("positions", [])

    async def import_account(self, account_id: str) -> list[dict]:
        """Fetch positions and return importable items.

        Returns list of {ticker, instrument_type, quantity, purchase_price}.
        Skips unsupported types and zero-quantity positions.
        Ticker is taken directly from the position data (T-Bank REST API includes it).
        """
        positions = await self.get_positions(account_id)
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
            })

        return items

    async def sync_portfolio(
        self,
        portfolio_id: int,
        account_id: str,
        bonds_only: bool,
        storage: "StorageService",
    ) -> dict:
        """Sync T-Bank positions into an existing portfolio.

        Returns {added, updated, removed_candidates, errors}.
        New/changed positions are written to DB. Disappeared positions are
        returned as removed_candidates — NOT auto-deleted.
        """
        from app.services.cache_service import cache_service

        # Fetch live positions from broker
        try:
            api_items = await self.import_account(account_id)
        except TBankError:
            raise
        except Exception as exc:
            raise TBankError(f"Ошибка получения позиций: {exc}") from exc

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
                    )
                    added += 1
                elif abs(db_item["quantity"] - api_item["quantity"]) > 1e-6:
                    # Quantity changed — update, preserve original purchase_price
                    storage.update_item(
                        item_id=db_item["id"],
                        portfolio_id=portfolio_id,
                        quantity=api_item["quantity"],
                        purchase_price=db_item["purchase_price"],
                    )
                    updated += 1
            except Exception as exc:
                logger.warning("sync_portfolio: error processing %s: %s", key, exc)
                errors.append(api_item["ticker"])

        # Positions present in DB but gone from broker
        for key, db_item in db_map.items():
            if key not in api_map:
                removed_candidates.append(db_item["ticker"])

        if added > 0 or updated > 0:
            cache_service.invalidate(portfolio_id)

        logger.info(
            "AUDIT tbank_sync: portfolio_id=%d added=%d updated=%d removed_candidates=%d errors=%d",
            portfolio_id, added, updated, len(removed_candidates), len(errors),
        )

        return {
            "added": added,
            "updated": updated,
            "removed_candidates": removed_candidates,
            "errors": errors,
        }
