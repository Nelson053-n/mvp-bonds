"""
Portfolio management API: CRUD operations and sharing.
"""

import uuid

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_portfolio_or_403
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

    storage_service.update_portfolio(
        portfolio_id,
        share_token=share_token,
        share_password_hash=share_password_hash,
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
