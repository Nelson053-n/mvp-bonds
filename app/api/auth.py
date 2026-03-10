"""
Authentication API routes: register, login, get current user.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.exceptions import AuthError
from app.services.auth_service import auth_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)
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
    plan: str = "free"
    plan_expires_at: int | None = None


class UserResponse(BaseModel):
    user_id: int
    username: str
    is_admin: bool = False
    plan: str = "free"
    plan_expires_at: int | None = None


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
async def login(payload: LoginInput, request: Request) -> dict:
    """Login with username and password."""
    client_ip = request.client.host if request.client else ""
    result = auth_service.login(payload.username, payload.password, client_ip)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверные учетные данные",
        )
    return result


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    """Get current authenticated user."""
    plan_info = storage_service.get_user_plan(current_user["sub"])
    return {
        "user_id": current_user["sub"],
        "username": current_user["username"],
        "is_admin": current_user.get("is_admin", False),
        "plan": plan_info["plan"],
        "plan_expires_at": plan_info["plan_expires_at"],
    }


class ChangePasswordInput(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


class ChangeEmailInput(BaseModel):
    email: str = Field(..., max_length=254)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordInput,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Change current user's password."""
    ok = auth_service.change_password(
        current_user["sub"], payload.old_password, payload.new_password
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный текущий пароль",
        )


class ChangeUsernameInput(BaseModel):
    new_username: str = Field(..., min_length=3, max_length=64)


@router.patch("/me/username", status_code=status.HTTP_204_NO_CONTENT)
async def change_username(
    payload: ChangeUsernameInput,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Change current user's username."""
    ok = auth_service.change_username(current_user["sub"], payload.new_username)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Имя пользователя уже занято",
        )


@router.post("/change-email")
async def change_email(
    payload: ChangeEmailInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Change current user's email. Returns smtp_available flag."""
    result = auth_service.change_email(current_user["sub"], payload.email)
    if not result["ok"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пользователь не найден")
    return {"smtp_available": result["smtp_available"]}


@router.get("/me/portfolios-stats")
async def get_my_portfolios_stats(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Get current user's portfolios with item counts."""
    portfolios = storage_service.get_portfolios_with_item_counts(current_user["sub"])
    user = storage_service.get_user_by_id(current_user["sub"])
    return {
        "email": user.get("email") if user else None,
        "tg_chat_id": user.get("tg_chat_id") if user else None,
        "portfolios": portfolios,
    }


class UpdateTelegramInput(BaseModel):
    tg_chat_id: str = Field(..., max_length=64)


@router.post("/me/telegram", status_code=status.HTTP_204_NO_CONTENT)
async def update_telegram(
    payload: UpdateTelegramInput,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Save user's personal Telegram Chat ID for password recovery."""
    storage_service.update_user_tg_chat_id(
        current_user["sub"], payload.tg_chat_id.strip() or None
    )


class ForgotPasswordInput(BaseModel):
    username: str = Field(..., min_length=1)
    lang: str = "ru"


class ResetPasswordInput(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6)


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordInput) -> dict:
    """Request password reset. Sends 6-digit code via Telegram and/or email."""
    method = await auth_service.request_password_reset(payload.username, lang=payload.lang)
    return {"method": method}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordInput) -> None:
    """Apply reset code and set a new password."""
    ok = auth_service.confirm_password_reset(payload.code, payload.new_password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный или просроченный код",
        )
