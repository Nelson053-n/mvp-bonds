"""
Admin API: user management, portfolio management, statistics.
Only accessible to users with is_admin=True.
"""

import bcrypt
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_admin_user
from app.services.storage_service import storage_service
from app.services.moex_service import moex_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class ChangePasswordInput(BaseModel):
    password: str = Field(..., min_length=6)


class SetAdminInput(BaseModel):
    is_admin: bool


class ToggleSourceInput(BaseModel):
    enabled: bool


# ── Stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(admin: dict = Depends(get_admin_user)) -> dict:
    """Global platform statistics."""
    return storage_service.get_stats()


# ── Data Sources ─────────────────────────────────────────────────────────────

@router.get("/data-sources")
async def get_data_sources(admin: dict = Depends(get_admin_user)) -> list:
    """Status of external data sources: MOEX price, MOEX rating, SmartLab."""
    return moex_service.get_sources_status()


@router.post("/data-sources/{source}/toggle")
async def toggle_data_source(
    source: str,
    payload: ToggleSourceInput,
    admin: dict = Depends(get_admin_user),
) -> dict:
    """Enable or disable an external data source."""
    ok = moex_service.set_source_enabled(source, payload.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Источник данных не найден")
    logger.info(
        "Admin %s %s data source %s",
        admin["username"],
        "enabled" if payload.enabled else "disabled",
        source,
    )
    return {"ok": True, "source": source, "enabled": payload.enabled}


@router.post("/data-sources/ratings/clear-cache")
async def clear_rating_cache(admin: dict = Depends(get_admin_user)) -> dict:
    """Clear in-memory rating cache so next refresh re-fetches all ratings."""
    count = len(moex_service._credit_rating_cache)
    moex_service._credit_rating_cache.clear()
    logger.info("Admin %s cleared rating cache (%d entries)", admin["username"], count)
    return {"ok": True, "cleared": count}


# ── Users ────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(admin: dict = Depends(get_admin_user)) -> list:
    """List all users with portfolio counts."""
    return storage_service.get_all_users()


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, admin: dict = Depends(get_admin_user)) -> dict:
    """Delete a user and all their data."""
    if user_id == admin["sub"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    user = storage_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    storage_service.delete_user(user_id)
    logger.info("Admin %s deleted user %d (%s)", admin["username"], user_id, user["username"])
    return {"ok": True}


@router.patch("/users/{user_id}/password")
async def change_user_password(
    user_id: int,
    payload: ChangePasswordInput,
    admin: dict = Depends(get_admin_user),
) -> dict:
    """Change any user's password."""
    user = storage_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    hash_ = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()
    storage_service.update_user_password(user_id, hash_)
    logger.info("Admin %s changed password for user %d (%s)", admin["username"], user_id, user["username"])
    return {"ok": True}


@router.patch("/users/{user_id}/role")
async def set_user_role(
    user_id: int,
    payload: SetAdminInput,
    admin: dict = Depends(get_admin_user),
) -> dict:
    """Grant or revoke admin role. The primary admin (id=1) cannot be demoted."""
    if user_id == 1 and not payload.is_admin:
        raise HTTPException(status_code=400, detail="Нельзя снять роль главного администратора")
    user = storage_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    storage_service.set_user_admin(user_id, payload.is_admin)
    action = "granted admin" if payload.is_admin else "revoked admin"
    logger.info("Admin %s %s for user %d (%s)", admin["username"], action, user_id, user["username"])
    return {"ok": True, "is_admin": payload.is_admin}


# ── Portfolios ───────────────────────────────────────────────────────────────

@router.get("/portfolios")
async def list_all_portfolios(admin: dict = Depends(get_admin_user)) -> list:
    """List all portfolios across all users."""
    return storage_service.get_all_portfolios_with_users()


@router.get("/users/{user_id}/portfolios")
async def list_user_portfolios(user_id: int, admin: dict = Depends(get_admin_user)) -> list:
    """List portfolios for a specific user."""
    user = storage_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return storage_service.get_portfolios(user_id)


@router.delete("/portfolios/{portfolio_id}")
async def delete_portfolio(portfolio_id: int, admin: dict = Depends(get_admin_user)) -> dict:
    """Delete any portfolio."""
    portfolio = storage_service.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")
    storage_service.delete_portfolio(portfolio_id)
    logger.info("Admin %s deleted portfolio %d", admin["username"], portfolio_id)
    return {"ok": True}


# ── Plan management ───────────────────────────────────────────────────────────

class SetPlanInput(BaseModel):
    plan: str = Field(..., pattern="^(free|pro)$")
    expires_at: int | None = None  # Unix timestamp; None = unlimited


@router.patch("/users/{user_id}/plan")
async def set_user_plan(
    user_id: int,
    payload: SetPlanInput,
    admin: dict = Depends(get_admin_user),
) -> dict:
    """Set plan (free/pro) for a user. expires_at=None means unlimited."""
    user = storage_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    storage_service.set_user_plan(user_id, payload.plan, payload.expires_at)
    logger.info(
        "Admin %s set plan=%s expires_at=%s for user %d (%s)",
        admin["username"], payload.plan, payload.expires_at, user_id, user["username"],
    )
    return {"ok": True}


# ── YooKassa settings ─────────────────────────────────────────────────────────

class YooKassaInput(BaseModel):
    shop_id: str = Field(default="", max_length=64)
    secret_key: str = Field(default="", max_length=256)


@router.get("/settings/yookassa")
async def get_yookassa(admin: dict = Depends(get_admin_user)) -> dict:
    """Get YooKassa configuration (stub)."""
    return {
        "shop_id": storage_service.get_setting("yookassa_shop_id") or "",
        "has_secret": bool(storage_service.get_setting("yookassa_secret_key")),
    }


@router.patch("/settings/yookassa")
async def save_yookassa(payload: YooKassaInput, admin: dict = Depends(get_admin_user)) -> dict:
    """Save YooKassa shop_id and optionally secret_key."""
    storage_service.set_setting("yookassa_shop_id", payload.shop_id)
    if payload.secret_key:
        storage_service.set_setting("yookassa_secret_key", payload.secret_key)
    logger.info("Admin %s updated YooKassa settings (shop_id=%s)", admin["username"], payload.shop_id)
    return {"ok": True}
