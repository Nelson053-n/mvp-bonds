"""
Portfolio management API: CRUD operations and sharing.
"""

import csv
import io
import time
import uuid

import bcrypt
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_portfolio_or_403
from app.config import settings as app_settings
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


class CreatePortfolioInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class UpdatePortfolioInput(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)


class PortfolioResponse(BaseModel):
    id: int
    user_id: int
    name: str
    share_token: str | None
    has_share_password: bool
    created_at: str


class PortfoliosListResponse(BaseModel):
    portfolios: list[PortfolioResponse]


class SharePortfolioInput(BaseModel):
    password: str | None = Field(None, min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class SharePortfolioResponse(BaseModel):
    share_url: str
    share_token: str


@router.get("", response_model=PortfoliosListResponse)
async def list_portfolios(current_user: dict = Depends(get_current_user)) -> dict:
    """List all portfolios for current user."""
    user_id = current_user["sub"]
    portfolios_data = storage_service.get_portfolios(user_id)

    return {
        "portfolios": [
            {
                "id": p["id"],
                "user_id": p["user_id"],
                "name": p["name"],
                "share_token": p["share_token"],
                "has_share_password": p["share_password_hash"] is not None,
                "created_at": p["created_at"],
            }
            for p in portfolios_data
        ]
    }


@router.post("", response_model=PortfolioResponse, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    payload: CreatePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a new portfolio for current user."""
    user_id = current_user["sub"]
    count = storage_service.count_portfolios(user_id)
    if count >= app_settings.max_portfolios_per_user:
        raise HTTPException(status_code=400, detail=f"Максимум {app_settings.max_portfolios_per_user} портфелей на аккаунт")
    portfolio_id = storage_service.create_portfolio(user_id, payload.name)
    portfolio = storage_service.get_portfolio(portfolio_id)

    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.get("/export-all")
async def export_all_portfolios(
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Export all portfolios as a single CSV with portfolio_name column."""
    user_id = current_user["sub"]
    portfolios = storage_service.get_portfolios(user_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["portfolio_name", "ticker", "instrument_type", "quantity", "purchase_price"])
    for p in portfolios:
        items = storage_service.get_items(p["id"])
        for item in items:
            writer.writerow([
                p["name"],
                item["ticker"],
                item["instrument_type"],
                item["quantity"],
                item["purchase_price"],
            ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=all_portfolios.csv"},
    )


@router.post("/import-all")
async def import_all_portfolios(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Import all portfolios from CSV. Missing portfolios are created automatically.

    Expected columns: portfolio_name, ticker, instrument_type, quantity, purchase_price.
    """
    user_id = current_user["sub"]

    _MAX_CSV_SIZE = 2 * 1024 * 1024  # 2 MB для экспорта всех портфелей
    content = await file.read(_MAX_CSV_SIZE + 1)
    if len(content) > _MAX_CSV_SIZE:
        raise HTTPException(status_code=413, detail="Файл слишком большой (максимум 2 МБ)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    required = {"portfolio_name", "ticker", "instrument_type", "quantity", "purchase_price"}

    added = 0
    errors: list[str] = []
    portfolio_cache: dict[str, int] = {}

    for p in storage_service.get_portfolios(user_id):
        portfolio_cache[p["name"]] = p["id"]

    for i, row in enumerate(reader, start=2):
        missing = required - set(row.keys())
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Отсутствуют колонки: {', '.join(missing)}",
            )

        portfolio_name = str(row["portfolio_name"]).strip()
        ticker = str(row["ticker"]).strip().upper()
        instrument_type = str(row["instrument_type"]).strip().lower()

        if not portfolio_name:
            errors.append(f"Строка {i}: пустое название портфеля")
            continue

        if instrument_type not in ("bond", "stock"):
            errors.append(f"Строка {i}: неверный тип '{instrument_type}'")
            continue

        try:
            quantity = float(row["quantity"])
            purchase_price = float(row["purchase_price"])
        except ValueError:
            errors.append(f"Строка {i}: неверные числовые значения")
            continue

        if quantity <= 0 or purchase_price <= 0:
            errors.append(f"Строка {i}: quantity и purchase_price должны быть > 0")
            continue

        if portfolio_name not in portfolio_cache:
            pid = storage_service.create_portfolio(user_id, portfolio_name)
            portfolio_cache[portfolio_name] = pid

        pid = portfolio_cache[portfolio_name]

        try:
            existing_item = storage_service.get_item_by_ticker(pid, ticker, instrument_type)
            if existing_item:
                storage_service.update_item(existing_item["id"], pid, quantity, purchase_price)
            else:
                storage_service.add_item(
                    ticker=ticker,
                    instrument_type=instrument_type,
                    quantity=quantity,
                    purchase_price=purchase_price,
                    portfolio_id=pid,
                )
            added += 1
        except Exception as exc:
            errors.append(f"Строка {i}: {exc}")

    if added > 0:
        for pid in portfolio_cache.values():
            cache_service.invalidate(pid)

    return {"added": added, "errors": len(errors), "error_details": errors}


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Get portfolio details."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.patch("/{portfolio_id}", response_model=PortfolioResponse)
async def update_portfolio(
    portfolio_id: int,
    payload: UpdatePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Update portfolio name."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    if payload.name:
        storage_service.update_portfolio(portfolio_id, name=payload.name)

    portfolio = storage_service.get_portfolio(portfolio_id)
    return {
        "id": portfolio["id"],
        "user_id": portfolio["user_id"],
        "name": portfolio["name"],
        "share_token": portfolio["share_token"],
        "has_share_password": portfolio["share_password_hash"] is not None,
        "created_at": portfolio["created_at"],
    }


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Delete a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)
    storage_service.delete_portfolio(portfolio_id)


@router.post("/{portfolio_id}/share", response_model=SharePortfolioResponse)
async def create_share_link(
    portfolio_id: int,
    payload: SharePortfolioInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a public share link for a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)

    # Generate unique share token
    share_token = str(uuid.uuid4())

    # Hash password if provided
    share_password_hash = None
    if payload.password:
        share_password_hash = bcrypt.hashpw(
            payload.password.encode(), bcrypt.gensalt()
        ).decode()

    expires_at = int(time.time()) + payload.expires_in_days * 86400 if payload.expires_in_days else None

    storage_service.update_portfolio(
        portfolio_id,
        share_token=share_token,
        share_password_hash=share_password_hash,
        share_expires_at=expires_at,
    )

    return {
        "share_token": share_token,
        "share_url": f"/share/{share_token}",
    }


@router.delete("/{portfolio_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(
    portfolio_id: int,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Revoke the public share link for a portfolio."""
    portfolio = await get_portfolio_or_403(portfolio_id, current_user)
    storage_service.update_portfolio(
        portfolio_id, share_token=None, share_password_hash=None
    )


@router.post("/{portfolio_id}/merge-into/{target_id}")
async def merge_portfolio(
    portfolio_id: int,
    target_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Move all items from source portfolio into target portfolio."""
    if portfolio_id == target_id:
        raise HTTPException(status_code=400, detail="Нельзя объединить портфель с самим собой")
    await get_portfolio_or_403(portfolio_id, current_user)
    await get_portfolio_or_403(target_id, current_user)
    moved = storage_service.merge_portfolios(portfolio_id, target_id)
    return {"moved": moved}


@router.get("/{portfolio_id}/snapshots")
async def get_snapshots(
    portfolio_id: int,
    days: int = 90,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """Get portfolio value history."""
    await get_portfolio_or_403(portfolio_id, current_user)
    if days not in (7, 30, 90, 365):
        days = 90
    return storage_service.get_portfolio_snapshots(portfolio_id, days)
