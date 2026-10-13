"""
FastAPI dependencies for authentication and portfolio access.
"""

import time
from urllib.parse import urlparse

import bcrypt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.auth_service import auth_service
from app.services.storage_service import storage_service

security = HTTPBearer(auto_error=False)

# Сессия браузера — в HttpOnly-cookie, а не в localStorage: XSS больше не может
# унести токен (аудит 12.10.2026, M8). Заголовок Authorization: Bearer остаётся
# для скриптов (manage.py, deploy.sh) и API-клиентов.
SESSION_COOKIE = "bondai_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _token_from_request(request: Request, credentials) -> tuple[str | None, bool]:
    """(токен, пришёл_из_cookie). Заголовок приоритетнее cookie."""
    if credentials is not None:
        return credentials.credentials, False
    token = request.cookies.get(SESSION_COOKIE)
    return (token, True) if token else (None, False)


def _cookie_request_is_same_origin(request: Request) -> bool:
    """CSRF-защита для изменяющих запросов, авторизованных cookie.

    Первый слой — SameSite=Lax (браузер не шлёт cookie в межсайтовых POST).
    Второй — Sec-Fetch-Site и Origin: межсайтовый запрос отклоняем, даже если
    браузер cookie всё же отправил. Bearer-заголовок CSRF не подвержен —
    чужая страница не может его подставить.
    """
    if request.method in _SAFE_METHODS:
        return True
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return False
    origin = request.headers.get("origin")
    if origin:
        return urlparse(origin).netloc == request.headers.get("host", "")
    return True


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """
    Extract and verify JWT token from Authorization header or session cookie.
    Raises 401 if token is invalid or expired.
    """
    token, via_cookie = _token_from_request(request, credentials)
    if token is None:
        # Как раньше у HTTPBearer: без учётных данных — 403.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authenticated")
    if via_cookie and not _cookie_request_is_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Запрос с чужого сайта отклонён")
    payload = auth_service.verify_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или истекший токен",
        )
    # Подпись и срок — ещё не всё: токен отзывается сменой пароля
    # (users.token_version), а права берутся из БД, не из токена. Иначе
    # разжалованный админ и захваченная сессия жили до истечения 72 ч
    # (аудит 12.10.2026, M5). Старые токены без "tv" — версия 0.
    from app.services.storage_service import storage_service
    user = storage_service.get_user_by_id(payload.get("sub"))
    if user is None or int(payload.get("tv", 0)) != user["token_version"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или истекший токен",
        )
    payload["is_admin"] = user["is_admin"]
    return payload  # {"sub": user_id, "username": ..., "is_admin": из БД}


def get_admin_user(current_user: dict = Depends(get_current_user)) -> dict:
    """Require authenticated admin user (is_admin — из БД, см. get_current_user)."""
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ запрещён")
    return current_user


def portfolio_limit_refusal(current_user: dict) -> str | None:
    """Текст отказа, если новый портфель превысит лимит тарифа; иначе None.

    Единая проверка для всех путей создания портфеля (создание, import-all,
    импорт из Т-Банка): по отдельности они обходили free-лимит (аудит
    12.10.2026, M6).
    """
    from app.config import settings as app_settings
    from app.services.storage_service import storage_service
    pro = user_is_pro(current_user)
    limit = app_settings.max_portfolios_per_user if pro else app_settings.free_max_portfolios
    if storage_service.count_portfolios(current_user["sub"]) < limit:
        return None
    if pro:
        return f"Максимум {limit} портфелей на аккаунт"
    return f"На бесплатном тарифе — до {limit} портфелей. Оформите Pro для большего."


def user_is_pro(current_user: dict) -> bool:
    """Effective Pro status read live from the DB (never trust the JWT)."""
    from app.services.storage_service import storage_service
    user = storage_service.get_user_by_id(current_user["sub"])
    return bool(user and user.get("is_pro"))


def require_pro(current_user: dict = Depends(get_current_user)) -> dict:
    """Dependency that 403s non-Pro users (for Pro-only endpoints)."""
    if not user_is_pro(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступно в тарифе Pro")
    return current_user


# Auto-sync is free for the first FREE_AUTOSYNC_DAYS after registration, then Pro-only.
FREE_AUTOSYNC_DAYS = 30


def user_can_autosync(user_id: int) -> bool:
    """Pro users always; free users only within the trial window from signup."""
    from datetime import datetime, timezone, timedelta
    from app.services.storage_service import storage_service
    user = storage_service.get_user_by_id(user_id)
    if not user:
        return False
    if user.get("is_pro"):
        return True
    created = user.get("created_at")
    if not created:
        return True  # unknown signup date → don't lock out
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - dt < timedelta(days=FREE_AUTOSYNC_DAYS)


async def get_optional_user(request: Request) -> dict | None:
    """
    Try to extract JWT token from Authorization header.
    Returns None if no token or invalid token (for public/share routes).
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = auth_service.verify_token(token)
    if payload is None:
        return None
    # Та же сверка версии и прав, что в get_current_user: отозванный сменой
    # пароля токен не должен приниматься и на маршрутах с опциональным входом.
    user = storage_service.get_user_by_id(payload.get("sub"))
    if user is None or int(payload.get("tv", 0)) != user["token_version"]:
        return None
    payload["is_admin"] = user["is_admin"]
    return payload


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


def get_shared_portfolio(
    request: Request,
    share_token: str,
    x_share_password: str | None = Header(None),
) -> dict:
    """Validate shared portfolio token, expiry, and password.

    Reusable dependency for all /share/{token}/... endpoints.
    Rate-limits password attempts per (share_token, client_ip) to make
    bcrypt-checked passwords infeasible to brute-force online.
    """
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")

    if portfolio.get("share_expires_at") and portfolio["share_expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Ссылка устарела")

    if portfolio["share_password_hash"]:
        if not x_share_password:
            raise HTTPException(status_code=403, detail="Требуется пароль")
        client_ip = request.client.host if request.client else ""
        # 10 attempts per 15 min per (token, ip)
        if client_ip and not storage_service.check_rate_limit(
            f"share_pw:{share_token}:{client_ip}", 900, 10
        ):
            raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуйте позже")
        if not bcrypt.checkpw(x_share_password.encode(), portfolio["share_password_hash"].encode()):
            raise HTTPException(status_code=403, detail="Неверный пароль")

    return portfolio
