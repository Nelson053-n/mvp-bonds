"""T-Bank Invest API import endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.config import settings as app_settings
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service
from app.services.tbank_service import TBankError, TBankService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tbank", tags=["tbank"])


class TBankTokenInput(BaseModel):
    token: str = Field(..., min_length=10, max_length=500)


class TBankPreviewInput(BaseModel):
    token: str = Field(..., min_length=10, max_length=500)
    account_id: str = Field(..., min_length=1, max_length=100)


class TBankImportInput(BaseModel):
    token: str = Field(..., min_length=10, max_length=500)
    account_id: str = Field(..., min_length=1, max_length=100)
    bonds_only: bool = False


def _unique_portfolio_name(user_id: int, base_name: str) -> str:
    """Return base_name, or base_name (2), (3), etc. if already taken."""
    existing = {p["name"] for p in storage_service.get_portfolios(user_id)}
    if base_name not in existing:
        return base_name
    for i in range(2, 100):
        candidate = f"{base_name} ({i})"
        if candidate not in existing:
            return candidate
    return f"{base_name} ({100})"


@router.post("/accounts")
async def list_tbank_accounts(
    payload: TBankTokenInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List T-Bank broker accounts for the given read-only token."""
    svc = TBankService(payload.token)
    try:
        accounts = await svc.get_accounts()
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Unexpected error listing T-Bank accounts")
        raise HTTPException(status_code=500, detail="Ошибка подключения к Т-Банк API") from exc
    return {"accounts": accounts}


@router.post("/preview")
async def preview_tbank_portfolio(
    payload: TBankPreviewInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Preview T-Bank account: count bonds and stocks before import."""
    svc = TBankService(payload.token)
    try:
        items = await svc.import_account(payload.account_id)
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Unexpected error previewing T-Bank portfolio")
        raise HTTPException(status_code=500, detail="Ошибка получения данных портфеля") from exc

    bonds = [i for i in items if i["instrument_type"] == "bond"]
    stocks = [i for i in items if i["instrument_type"] == "stock"]

    return {
        "total": len(items),
        "bonds": len(bonds),
        "stocks": len(stocks),
    }


@router.post("/import")
async def import_tbank_portfolio(
    payload: TBankImportInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Import a T-Bank account as a new portfolio."""
    user_id = current_user["sub"]

    count = storage_service.count_portfolios(user_id)
    if count >= app_settings.max_portfolios_per_user:
        raise HTTPException(
            status_code=400,
            detail=f"Максимум {app_settings.max_portfolios_per_user} портфелей на аккаунт",
        )

    svc = TBankService(payload.token)

    try:
        accounts = await svc.get_accounts()
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Unexpected error listing T-Bank accounts")
        raise HTTPException(status_code=500, detail="Ошибка подключения к Т-Банк API") from exc

    account = next((a for a in accounts if a["id"] == payload.account_id), None)
    if account is None:
        raise HTTPException(status_code=400, detail="Счёт не найден")

    base_name = f"Т-Банк: {account['name']}" if account["name"] else "Т-Банк"
    portfolio_name = _unique_portfolio_name(user_id, base_name)

    try:
        items = await svc.import_account(payload.account_id)
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Unexpected error importing T-Bank portfolio")
        raise HTTPException(status_code=500, detail="Ошибка получения данных портфеля") from exc

    if payload.bonds_only:
        items = [i for i in items if i["instrument_type"] == "bond"]

    if not items:
        raise HTTPException(status_code=400, detail="В счёте нет подходящих бумаг для импорта")

    if len(items) > app_settings.max_items_per_portfolio:
        items = items[: app_settings.max_items_per_portfolio]

    portfolio_id = storage_service.create_portfolio(user_id, portfolio_name)

    added = 0
    errors: list[str] = []
    for item in items:
        try:
            storage_service.add_item(
                ticker=item["ticker"],
                instrument_type=item["instrument_type"],
                quantity=item["quantity"],
                purchase_price=item["purchase_price"],
                portfolio_id=portfolio_id,
            )
            added += 1
        except Exception as exc:
            logger.warning("Failed to add item %s: %s", item["ticker"], exc)
            errors.append(item["ticker"])

    if added > 0:
        cache_service.invalidate(portfolio_id)

    logger.info(
        "AUDIT tbank_import: user_id=%d account_id=%s portfolio_id=%d added=%d errors=%d",
        user_id, payload.account_id, portfolio_id, added, len(errors),
    )

    return {
        "portfolio_id": portfolio_id,
        "portfolio_name": portfolio_name,
        "added": added,
        "errors": len(errors),
        "error_details": errors,
    }
