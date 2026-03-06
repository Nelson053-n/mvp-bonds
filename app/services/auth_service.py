"""
Authentication service for user registration, login, and JWT token management.
"""

import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings
from app.exceptions import AuthError
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self) -> None:
        if not settings.jwt_secret:
            raise ValueError("MVP_JWT_SECRET is required")

    def register(self, username: str, password: str) -> dict:
        """
        Register a new user.
        Raises AuthError if username already exists.
        """
        if not username or not password:
            raise AuthError("Имя пользователя и пароль обязательны")

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        try:
            user_id = storage_service.create_user(username, password_hash)
        except ValueError as e:
            raise AuthError(str(e)) from e

        # Create default portfolio for new user
        try:
            storage_service.create_portfolio(user_id, "Основной портфель")
            logger.info("Default portfolio created for user: %s (id=%d)", username, user_id)
        except Exception as e:
            logger.warning("Failed to create default portfolio for user %d: %s", user_id, e)

        logger.info("User registered: %s (id=%d)", username, user_id)
        return {
            "user_id": user_id,
            "username": username,
            "is_admin": False,
            "access_token": self.create_token(user_id, username, False),
        }

    def login(self, username: str, password: str) -> dict | None:
        """
        Authenticate a user by username and password.
        Returns token dict on success, None on failure.
        """
        user = storage_service.get_user_by_username(username)
        if not user:
            return None

        if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return None

        logger.info("User logged in: %s (id=%d)", username, user["id"])
        is_admin = bool(user.get("is_admin", False))
        token = self.create_token(user["id"], user["username"], is_admin)
        return {
            "user_id": user["id"],
            "username": user["username"],
            "is_admin": is_admin,
            "access_token": token,
        }

    def create_token(self, user_id: int, username: str, is_admin: bool = False) -> str:
        """
        Create a JWT access token.
        """
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=settings.jwt_expiry_hours)

        payload = {
            "sub": str(user_id),  # Must be string per JWT spec
            "username": username,
            "is_admin": is_admin,
            "exp": int(exp.timestamp()),
            "iat": int(now.timestamp()),
        }

        token = jwt.encode(
            payload,
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        return token

    def verify_token(self, token: str) -> dict | None:
        """
        Verify a JWT token and return its payload.
        Returns None if token is invalid or expired.
        """
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            # Convert sub back to int (stored as string in JWT per spec)
            payload["sub"] = int(payload["sub"])
            payload["is_admin"] = bool(payload.get("is_admin", False))
            return payload
        except jwt.ExpiredSignatureError:
            logger.debug("Token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("Invalid token: %s", e)
            return None


auth_service = AuthService()
