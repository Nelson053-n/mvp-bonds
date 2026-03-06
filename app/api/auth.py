"""
Authentication API routes: register, login, get current user.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.exceptions import AuthError
from app.services.auth_service import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterInput(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6)


class LoginInput(BaseModel):
    username: str = Field(...)
    password: str = Field(...)


class AuthResponse(BaseModel):
    access_token: str
    user_id: int
    username: str
    is_admin: bool = False


class UserResponse(BaseModel):
    user_id: int
    username: str
    is_admin: bool = False


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterInput) -> dict:
    """Register a new user."""
    try:
        result = auth_service.register(payload.username, payload.password)
        return result
    except AuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.detail,
        ) from e


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginInput) -> dict:
    """Login with username and password."""
    result = auth_service.login(payload.username, payload.password)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверные учетные данные",
        )
    return result


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    """Get current authenticated user."""
    return {
        "user_id": current_user["sub"],
        "username": current_user["username"],
        "is_admin": current_user.get("is_admin", False),
    }
