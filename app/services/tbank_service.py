"""T-Bank Invest API integration for portfolio import.

Uses T-Bank REST API (no SDK). Token is passed per-request, never stored.
"""

import logging

import httpx

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
