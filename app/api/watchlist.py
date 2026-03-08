"""Watchlist API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.services.storage_service import storage_service
from app.services.moex_service import moex_service

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class AddWatchlistInput(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32)
    instrument_type: str = Field(default="bond", pattern="^(bond|stock)$")
    note: str | None = Field(None, max_length=200)


@router.get("")
async def get_watchlist(current_user: dict = Depends(get_current_user)) -> dict:
    """Get user's watchlist."""
    items = storage_service.get_watchlist(current_user["sub"])
    # Enrich with current prices from MOEX
    enriched = []
    for item in items:
        data = {
            "id": item["id"],
            "ticker": item["ticker"],
            "instrument_type": item["instrument_type"],
            "note": item["note"],
            "created_at": item["created_at"],
            "current_price": None,
            "name": None,
        }
        try:
            bond_data = await moex_service.get_bond_snapshot(item["ticker"])
            if bond_data:
                data["current_price"] = bond_data.clean_price_percent
                data["name"] = bond_data.name
        except Exception:
            pass
        enriched.append(data)
    return {"items": enriched}


@router.post("")
async def add_to_watchlist(
    payload: AddWatchlistInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Add ticker to watchlist."""
    ticker = payload.ticker.strip().upper()
    watchlist_id = storage_service.add_to_watchlist(
        current_user["sub"], ticker, payload.instrument_type, payload.note
    )
    return {"id": watchlist_id, "ticker": ticker}


@router.delete("/{watchlist_id}")
async def remove_from_watchlist(
    watchlist_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Remove item from watchlist."""
    deleted = storage_service.remove_from_watchlist(current_user["sub"], watchlist_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    return {"deleted": True}
