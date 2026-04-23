"""T-Bank Invest API import and auto-sync endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.config import settings as app_settings
from app.services.cache_service import cache_service
from app.services.crypto_utils import encrypt_token
from app.services.storage_service import storage_service
from app.services.tbank_service import TBankError, TBankService
from app.services.tbank_sync_service import do_sync_one, parse_pending_removal

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
                source="tbank",
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


# ── Auto-sync models ─────────────────────────────────────────────────────────

class TBankSyncEnableInput(BaseModel):
    portfolio_id: int
    token: str = Field(..., min_length=10, max_length=500)
    account_id: str = Field(..., min_length=1, max_length=100)
    bonds_only: bool = False


class TBankSyncDisableInput(BaseModel):
    portfolio_id: int


class TBankSyncNowInput(BaseModel):
    portfolio_id: int


class TBankConfirmRemovalInput(BaseModel):
    portfolio_id: int
    tickers: list[Annotated[str, Field(max_length=50)]] = Field(..., max_length=200)
    confirm: bool


def _get_portfolio_or_403(portfolio_id: int, user_id: int) -> dict:
    """Return portfolio dict or raise 403/404."""
    portfolio = storage_service.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")
    if portfolio["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Нет доступа к портфелю")
    return portfolio


# ── Auto-sync endpoints ──────────────────────────────────────────────────────

@router.post("/sync/enable")
async def tbank_sync_enable(
    payload: TBankSyncEnableInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Enable auto-sync for a portfolio: validate token, save encrypted, run first sync."""
    user_id = current_user["sub"]
    _get_portfolio_or_403(payload.portfolio_id, user_id)

    # Validate token
    svc = TBankService(payload.token)
    try:
        await svc.get_accounts()
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("Unexpected error validating T-Bank token for sync/enable")
        raise HTTPException(status_code=500, detail="Ошибка подключения к Т-Банк API") from exc

    # Encrypt and persist
    token_enc = encrypt_token(payload.token, app_settings.jwt_secret)
    storage_service.upsert_sync_config(
        portfolio_id=payload.portfolio_id,
        tbank_token_enc=token_enc,
        tbank_token_prefix=payload.token[:4],
        tbank_account_id=payload.account_id,
        bonds_only=payload.bonds_only,
    )

    # Immediate first sync
    cfg = storage_service.get_sync_config(payload.portfolio_id)
    result: dict = {}
    sync_error: str | None = None
    try:
        result = await do_sync_one(payload.portfolio_id, cfg)
    except TBankError as exc:
        sync_error = exc.message
    except Exception as exc:
        sync_error = str(exc)

    cfg = storage_service.get_sync_config(payload.portfolio_id)
    return {
        "ok": True,
        "last_sync_at": cfg["last_sync_at"] if cfg else None,
        "masked_token": payload.token[:4] + "***",
        "added": result.get("added", 0),
        "updated": result.get("updated", 0),
        "removed_candidates": result.get("removed_candidates", []),
        "sync_error": sync_error,
    }


@router.get("/sync/status")
async def tbank_sync_status(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return current sync configuration and status for a portfolio."""
    user_id = current_user["sub"]
    _get_portfolio_or_403(portfolio_id, user_id)

    cfg = storage_service.get_sync_config(portfolio_id)
    if not cfg:
        return {"enabled": False}

    pending_removal = parse_pending_removal(cfg.get("last_sync_error"))
    last_error = cfg["last_sync_error"]
    if last_error and last_error.startswith("PENDING_REMOVAL:"):
        last_error = None  # Don't expose raw prefix to UI

    return {
        "enabled": cfg["sync_enabled"],
        "masked_token": cfg["tbank_token_prefix"] + "***",
        "account_id": cfg["tbank_account_id"],
        "bonds_only": cfg["bonds_only"],
        "last_sync_at": cfg["last_sync_at"],
        "last_sync_error": last_error,
        "pending_removal": pending_removal,
    }


@router.post("/sync/disable")
async def tbank_sync_disable(
    payload: TBankSyncDisableInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Disable auto-sync for a portfolio."""
    user_id = current_user["sub"]
    _get_portfolio_or_403(payload.portfolio_id, user_id)

    cfg = storage_service.get_sync_config(payload.portfolio_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Синхронизация не настроена")

    storage_service.set_sync_enabled(payload.portfolio_id, False)
    logger.info("AUDIT tbank_sync_disable: user_id=%d portfolio_id=%d", user_id, payload.portfolio_id)
    return {"ok": True}


@router.post("/sync/now")
async def tbank_sync_now(
    payload: TBankSyncNowInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Trigger an immediate sync for an already-configured portfolio."""
    user_id = current_user["sub"]
    _get_portfolio_or_403(payload.portfolio_id, user_id)

    cfg = storage_service.get_sync_config(payload.portfolio_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Синхронизация не настроена")
    if not cfg["sync_enabled"]:
        raise HTTPException(status_code=400, detail="Синхронизация отключена")

    try:
        result = await do_sync_one(payload.portfolio_id, cfg)
    except TBankError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        logger.exception("tbank_sync_now failed for portfolio_id=%d", payload.portfolio_id)
        raise HTTPException(status_code=500, detail="Ошибка синхронизации") from exc

    cfg = storage_service.get_sync_config(payload.portfolio_id)
    return {
        "ok": True,
        "last_sync_at": cfg["last_sync_at"] if cfg else None,
        "added": result.get("added", 0),
        "updated": result.get("updated", 0),
        "removed_candidates": result.get("removed_candidates", []),
        "skipped": result.get("skipped", False),
    }


@router.post("/sync/confirm-removal")
async def tbank_sync_confirm_removal(
    payload: TBankConfirmRemovalInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Confirm or reject removal of positions that disappeared from broker."""
    user_id = current_user["sub"]
    _get_portfolio_or_403(payload.portfolio_id, user_id)

    cfg = storage_service.get_sync_config(payload.portfolio_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Синхронизация не настроена")

    removed = 0
    if payload.confirm and payload.tickers:
        removed = storage_service.soft_delete_tbank_items(payload.portfolio_id, payload.tickers)
        if removed > 0:
            cache_service.invalidate(payload.portfolio_id)

    # Clear PENDING_REMOVAL from last_sync_error
    current_error = cfg.get("last_sync_error", "") or ""
    if current_error.startswith("PENDING_REMOVAL:"):
        storage_service.update_sync_status(
            payload.portfolio_id,
            cfg["last_sync_at"] or "",
            None,
        )

    logger.info(
        "AUDIT tbank_confirm_removal: user_id=%d portfolio_id=%d confirm=%s removed=%d",
        user_id, payload.portfolio_id, payload.confirm, removed,
    )
    return {"ok": True, "removed": removed}
