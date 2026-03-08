"""
FastAPI dependencies for authentication and portfolio access.
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.auth_service import auth_service
from app.services.storage_service import storage_service

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Extract and verify JWT token from Authorization header.
    Raises 401 if token is invalid or expired.
    """
    payload = auth_service.verify_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или истекший токен",
        )
    return payload  # {"sub": user_id, "username": ...}


def get_admin_user(current_user: dict = Depends(get_current_user)) -> dict:
    """Require authenticated admin user."""
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ запрещён")
    return current_user


async def get_optional_user(request: Request) -> dict | None:
    """
    Try to extract JWT token from Authorization header.
    Returns None if no token or invalid token (for public/share routes).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    return auth_service.verify_token(token)


async def get_portfolio_or_403(portfolio_id: int, current_user: dict) -> dict:
    """
    Check if portfolio exists and belongs to current user.
    Raises 403 if not owned by user, 404 if not found.
    """
    portfolio = storage_service.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Портфель не найден",
        )

    if portfolio["user_id"] != current_user["sub"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ запрещен",
        )

    return portfolio
