"""
Authentication API routes: register, login, get current user.
"""

import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import SESSION_COOKIE, get_current_user
from app.config import settings
from app.exceptions import AuthError
from app.services.auth_service import auth_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Telegram chat_id: число, для групп/каналов со знаком минус.
_CHAT_ID_RE = re.compile(r"-?\d{1,32}")


# Буквы (латиница, кириллица), цифры и _ . @ + -. Имя попадает в сообщения
# бота и письма: без белого списка туда уходили теги и ссылки (аудит
# 12.10.2026, M2). Проверено на проде: все кроме одного из 142 имён проходят.
_USERNAME_PATTERN = r"^[A-Za-zА-Яа-яЁё0-9_.@+\-]+$"


class RegisterInput(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=_USERNAME_PATTERN)
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
    is_pro: bool = False
    pro_until: str | None = None  # ISO date; None = lifetime (or not Pro)
    has_email: bool = False  # адрес не отдаём, только факт: фронт предлагает его добавить


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Сессия браузера — HttpOnly-cookie; JS её не видит (аудит 12.10.2026, M8)."""
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=settings.jwt_expiry_hours * 3600,
        httponly=True,
        samesite="lax",
        # За nginx (--proxy-headers, X-Forwarded-Proto) схема — https; локально
        # по http Secure-cookie браузер бы не сохранил.
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterInput, request: Request, response: Response) -> dict:
    """Register a new user."""
    # Без лимита аноним создавал аккаунты (каждый — с триалом Pro) и перебирал
    # занятые имена по ответу «уже существует» (аудит 12.10.2026, M9).
    client_ip = request.client.host if request.client else ""
    if client_ip and not storage_service.check_rate_limit(
            f"register:ip:{client_ip}", 3600, settings.register_max_per_hour):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много регистраций. Попробуйте позже",
        )
    try:
        result = auth_service.register(payload.username, payload.password)
        _set_session_cookie(response, request, result["access_token"])
        return result
    except AuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.detail,
        ) from e


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginInput, request: Request, response: Response) -> dict:
    """Login with username and password."""
    client_ip = request.client.host if request.client else ""
    result = auth_service.login(payload.username, payload.password, client_ip)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверные учетные данные",
        )
    _set_session_cookie(response, request, result["access_token"])
    return result


@router.post("/session", status_code=status.HTTP_204_NO_CONTENT)
async def exchange_session(
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Перенести сессию из заголовка Bearer в cookie.

    Для браузеров, где токен остался в localStorage от прежней версии: страница
    один раз обменивает его на cookie и удаляет — без повторного входа.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нужен заголовок Authorization")
    _set_session_cookie(response, request, auth_header[7:])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    """Удалить cookie сессии (токен в localStorage больше не хранится)."""
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    """Get current authenticated user."""
    # Pro status isn't in the JWT (it can change mid-session) — read it live.
    user = storage_service.get_user_by_id(current_user["sub"])
    return {
        "user_id": current_user["sub"],
        "username": current_user["username"],
        "is_admin": current_user.get("is_admin", False),
        "is_pro": bool(user.get("is_pro")) if user else False,
        "pro_until": (user.get("pro_until") if user else None),
        "has_email": bool(user.get("email")) if user else False,
    }


class ChangePasswordInput(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


class ChangeEmailInput(BaseModel):
    # Пусто — отвязка; иначе минимальная проверка формата: адрес уходит в
    # SMTP-заголовок To и получает письмо от bondai (аудит M4).
    email: str = Field(..., max_length=254, pattern=r"^$|^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordInput,
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Change current user's password.

    Смена пароля отзывает все выданные токены (token_version), поэтому
    возвращаем новый — иначе пользователя выкинуло бы из его же сессии.
    """
    ok = auth_service.change_password(
        current_user["sub"], payload.old_password, payload.new_password
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный текущий пароль",
        )
    user = storage_service.get_user_by_id(current_user["sub"])
    token = auth_service.create_token(
        user["id"], user["username"], user["is_admin"], user["token_version"])
    _set_session_cookie(response, request, token)
    return {"access_token": token}


class ChangeUsernameInput(BaseModel):
    new_username: str = Field(..., min_length=3, max_length=32, pattern=_USERNAME_PATTERN)


@router.patch("/me/username", status_code=status.HTTP_204_NO_CONTENT)
async def change_username(
    payload: ChangeUsernameInput,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Change current user's username."""
    # Ответ 409/204 раскрывает, занято ли имя: без лимита это перебор логинов
    # в обход лимита регистраций (аудит M9).
    if not storage_service.check_rate_limit(f"change_username:{current_user['sub']}", 3600, 10):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Попробуйте позже",
        )
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
    # Каждая смена шлёт письмо на указанный адрес: без лимита — спам с нашего
    # SMTP и исчерпание квоты, после которого не доходят коды сброса (аудит M4).
    if not storage_service.check_rate_limit(f"change_email:{current_user['sub']}", 3600, 5):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Попробуйте позже",
        )
    result = await auth_service.change_email(current_user["sub"], payload.email)
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
        # Telegram отвязали мы, а не пользователь: фронт показывает баннер.
        "tg_chat_id_reset": bool(user.get("tg_chat_id_reset")) if user else False,
        "portfolios": portfolios,
    }


class UpdateTelegramInput(BaseModel):
    tg_chat_id: str = Field(..., max_length=64)

    @field_validator("tg_chat_id")
    @classmethod
    def _numeric_chat_id(cls, v: str) -> str:
        """Chat ID — только число: @username Bot API не принимает.

        Введённый «@user» уходил в sendMessage как есть и возвращал 400
        «chat not found» при каждой рассылке, а сам адрес молча оставался
        в базе. Пустая строка допустима — это отвязка Telegram.
        """
        v = v.strip()
        if not v:
            return v
        if not _CHAT_ID_RE.fullmatch(v):
            raise ValueError(
                "Chat ID — это число (например 123456789), а не @username. "
                "Узнать свой ID: напишите @userinfobot в Telegram."
            )
        return v


@router.post("/me/telegram", status_code=status.HTTP_204_NO_CONTENT)
async def update_telegram(
    payload: UpdateTelegramInput,
    current_user: dict = Depends(get_current_user),
) -> None:
    """Отвязать Telegram (пустой chat_id). Привязка — только через /me/telegram/link.

    Ручной ввод chat_id позволял привязать к своему аккаунту чужой чат и
    слать туда сообщения от имени бота (аудит 12.10.2026, M2).
    """
    if payload.tg_chat_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Подключайте Telegram кнопкой «Подключить Telegram» в настройках.",
        )
    storage_service.update_user_tg_chat_id(current_user["sub"], None)


_TG_LINK_TTL = 900


@router.post("/me/telegram/link")
async def create_telegram_link(current_user: dict = Depends(get_current_user)) -> dict:
    """Ссылка на бота с одноразовым кодом: бот привяжет чат, где нажали Start."""
    if not storage_service.check_rate_limit(f"tg_link:{current_user['sub']}", 900, 10):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Попробуйте позже",
        )
    nonce = secrets.token_urlsafe(16)
    storage_service.create_tg_link_nonce(
        current_user["sub"], nonce, int(time.time()) + _TG_LINK_TTL)
    return {"url": f"https://t.me/{settings.tg_bot_username}?start=link_{nonce}"}


@router.get("/me/telegram/link/status")
async def telegram_link_status(current_user: dict = Depends(get_current_user)) -> dict:
    """Ждёт ли ссылка привязки нажатия Start (код ещё не погашен ботом)."""
    user = storage_service.get_user_by_id(current_user["sub"]) or {}
    return {
        "pending": storage_service.has_tg_link_nonce(current_user["sub"], int(time.time())),
        "tg_chat_id": user.get("tg_chat_id"),
    }


class ForgotPasswordInput(BaseModel):
    username: str = Field(..., min_length=1)
    lang: str = "ru"


class ResetPasswordInput(BaseModel):
    # Логин вместе с кодом: код сверяется только с кодами этого пользователя.
    # Без него любые 5 неверных кодов гасили сбросы всех пользователей.
    username: str = Field(..., min_length=1, max_length=64)
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6)


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordInput, request: Request) -> dict:
    """Request password reset. Sends 6-digit code via Telegram and/or email.

    Response intentionally does not disclose whether the username exists.
    """
    client_ip = request.client.host if request.client else ""
    # Cap forgot-password requests per IP to slow username enumeration sweeps.
    if client_ip and not storage_service.check_rate_limit(
        f"forgot:ip:{client_ip}", 900, 20
    ):
        logger.warning("forgot-password rate limit exceeded ip=%s", client_ip)
        return {"method": "sent"}
    method = await auth_service.request_password_reset(payload.username, lang=payload.lang)
    return {"method": method}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordInput, request: Request) -> None:
    """Apply reset code and set a new password."""
    client_ip = request.client.host if request.client else ""
    # Блокировку IP проверяем ДО сверки кода. Раньше код сверялся первым, а
    # заблокированный IP получал 429 без списания попыток у кодов — то есть
    # перебирал коды без ограничений (аудит 12.10.2026, H1).
    if auth_service.is_reset_confirm_blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Попробуйте позже",
        )
    ok = auth_service.confirm_password_reset(
        payload.username.strip(), payload.code, payload.new_password, client_ip)
    if ok:
        return None
    # Неудача: занять слот IP-окна и списать попытку у живых кодов этого
    # пользователя — всегда, даже если IP только что упёрся в лимит.
    auth_service.is_reset_confirm_rate_limited(client_ip)
    auth_service.register_failed_reset_attempt(payload.username.strip())
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Неверный или просроченный код",
    )
