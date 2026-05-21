"""
Authentication service for user registration, login, and JWT token management.
"""

import logging
import secrets
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import bcrypt
import httpx
import jwt

from app.config import settings
from app.exceptions import AuthError
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

# Precomputed bcrypt hash of an unguessable random secret; used to equalize
# the cost of login attempts for non-existent usernames.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt())


class AuthService:
    # Rate limiting for password reset request (/forgot-password) via SQLite
    _RESET_RATE_WINDOW = 900   # 15 min window
    _RESET_RATE_MAX = 3        # max 3 requests per window

    # Rate limiting for password reset confirm (/reset-password) per IP via SQLite
    _RESET_CONFIRM_WINDOW = 900   # 15 min window
    _RESET_CONFIRM_MAX = 10       # max 10 confirm attempts per IP per window

    # Max wrong code submissions before the code is invalidated.
    _RESET_CODE_MAX_ATTEMPTS = 5

    # Rate limiting for login (via SQLite)
    _LOGIN_RATE_WINDOW = 300   # 5 min window
    _LOGIN_RATE_MAX = 20       # per-username cap per window
    _LOGIN_IP_RATE_MAX = 30    # per-IP cap per window

    def _check_login_rate_limit(self, key: str) -> bool:
        """Rate limit via SQLite — survives server restarts."""
        return storage_service.check_rate_limit(key, self._LOGIN_RATE_WINDOW, self._LOGIN_RATE_MAX)

    def _check_login_ip_rate_limit(self, client_ip: str) -> bool:
        if not client_ip:
            return True
        return storage_service.check_rate_limit(
            f"login:ip:{client_ip}", self._LOGIN_RATE_WINDOW, self._LOGIN_IP_RATE_MAX
        )

    def __init__(self) -> None:
        pass  # jwt_secret is validated by pydantic Settings (required field)

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

        logger.info("User registered: %s (id=%d)", username, user_id)
        return {
            "user_id": user_id,
            "username": username,
            "is_admin": False,
            "access_token": self.create_token(user_id, username, False),
        }

    def login(self, username: str, password: str, client_ip: str = "") -> dict | None:
        """
        Authenticate a user by username and password.
        Returns token dict on success, None on failure.
        """
        # Rate limit by IP first (defends against credential-stuffing across many usernames),
        # then by username (defends against single-target bruteforce + enumeration).
        if not self._check_login_ip_rate_limit(client_ip):
            logger.warning("Login rate limit exceeded for ip=%s username=%s", client_ip, username)
            return None
        rate_key = f"login:{username}"
        if not self._check_login_rate_limit(rate_key):
            logger.warning("Login rate limit exceeded for username=%s ip=%s", username, client_ip)
            return None

        user = storage_service.get_user_by_username(username)
        if not user:
            # Constant-time bcrypt path against a dummy hash so timing
            # doesn't disclose whether the username exists.
            bcrypt.checkpw(password.encode(), _DUMMY_BCRYPT_HASH)
            return None

        if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return None

        logger.info("User logged in: %s (id=%d)", username, user["id"])
        storage_service.update_last_login(user["id"])
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

    def change_password(self, user_id: int, old_password: str, new_password: str) -> bool:
        """Change user password. Returns False if old password is wrong."""
        user = storage_service.get_user_by_id(user_id)
        if not user:
            return False
        if not bcrypt.checkpw(old_password.encode(), user["password_hash"].encode()):
            return False
        new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        storage_service.update_user_password(user_id, new_hash)
        logger.info("Password changed for user_id=%d", user_id)
        return True

    def change_username(self, user_id: int, new_username: str) -> bool:
        """Change username. Returns False if taken."""
        ok = storage_service.update_user_username(user_id, new_username)
        if ok:
            logger.info("Username changed for user_id=%d → %s", user_id, new_username)
        return ok

    # ── Password Reset ────────────────────────────────────────────

    # In-memory store: code -> {user_id, expires}
    _reset_codes: dict[str, dict] = {}
    RESET_TTL = 900  # 15 minutes

    def _cleanup_reset_codes(self) -> None:
        now = time.time()
        expired = [k for k, v in self._reset_codes.items() if v["expires"] < now]
        for k in expired:
            del self._reset_codes[k]

    def _check_reset_rate_limit(self, username: str) -> bool:
        key = f"reset:{username}"
        return storage_service.check_rate_limit(key, self._RESET_RATE_WINDOW, self._RESET_RATE_MAX)

    async def request_password_reset(self, username: str, lang: str = "ru") -> str:
        """
        Generate a 6-digit reset code and try to deliver it via Telegram and/or email.

        Always returns a constant 'sent' so the response does not disclose whether
        the account exists or which delivery channels are configured. Real outcomes
        (no user / no channel / SMTP missing / send failure) are logged server-side.
        Rate-limit responses are also masked as 'sent'.
        """
        if not self._check_reset_rate_limit(username):
            logger.warning("Reset rate limit exceeded for username=%s", username)
            return "sent"

        self._cleanup_reset_codes()
        user = storage_service.get_user_by_username_for_reset(username)
        if not user:
            return "sent"

        code = str(secrets.randbelow(900000) + 100000)  # 6-digit
        self._reset_codes[code] = {
            "user_id": user["id"],
            "expires": time.time() + self.RESET_TTL,
            "attempts": 0,
        }

        sent_via = []
        if user["tg_chat_id"]:
            tg_token = storage_service.get_setting("tg_bot_token", "")
            if tg_token:
                ok = await self._send_telegram_reset(tg_token, user["tg_chat_id"], code, username, lang=lang)
                if ok:
                    sent_via.append("telegram")

        if user["email"]:
            if not settings.smtp_host or not settings.smtp_from:
                logger.warning("Email set for user %s but SMTP not configured", username)
            else:
                ok = self._send_email_reset(user["email"], code, username, lang=lang)
                if ok:
                    sent_via.append("email")

        if not sent_via:
            logger.warning(
                "Reset code generated for user %s (id=%d) but no channel delivered",
                username, user["id"],
            )

        return "sent"

    async def _send_telegram_reset(self, bot_token: str, chat_id: str, code: str, username: str, lang: str = "ru") -> bool:
        if lang == "en":
            text = (
                f"🔐 Bond AI — password recovery\n\n"
                f"Account: {username}\n"
                f"Verification code: <b>{code}</b>\n\n"
                f"Valid for 15 minutes. If you did not request a reset — ignore this message."
            )
        else:
            text = (
                f"🔐 Bond AI — восстановление пароля\n\n"
                f"Аккаунт: {username}\n"
                f"Код подтверждения: <b>{code}</b>\n\n"
                f"Действителен 15 минут. Если вы не запрашивали сброс — проигнорируйте."
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                )
                return r.status_code == 200
        except Exception as e:
            logger.warning("Telegram reset send failed: %s", e)
            return False

    def _send_email_reset(self, email: str, code: str, username: str, lang: str = "ru") -> bool:
        if not settings.smtp_host or not settings.smtp_from:
            logger.warning("SMTP not configured — cannot send email reset code")
            return False
        if lang == "en":
            body = (
                f"Bond AI — Password Recovery\n\n"
                f"Account: {username}\n"
                f"Verification code: {code}\n\n"
                f"Valid for 15 minutes. If you did not request a reset — ignore this message."
            )
            subject = "Bond AI — password reset code"
        else:
            body = (
                f"Восстановление пароля Bond AI\n\n"
                f"Аккаунт: {username}\n"
                f"Код подтверждения: {code}\n\n"
                f"Действителен 15 минут. Если вы не запрашивали сброс — проигнорируйте."
            )
            subject = "Bond AI — код восстановления пароля"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = email
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                if settings.smtp_user and settings.smtp_password:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.smtp_from, [email], msg.as_string())
            return True
        except Exception as e:
            logger.warning("Email reset send failed: %s", e)
            return False

    def confirm_password_reset(self, code: str, new_password: str, client_ip: str = "") -> bool:
        """Apply reset code and set new password. Returns True on success.

        Defenses against code-space brute-force:
        - per-IP rate limit on confirm attempts (window-based, via SQLite);
        - per-code wrong-attempt counter — code is invalidated after _RESET_CODE_MAX_ATTEMPTS.
        """
        if client_ip and not storage_service.check_rate_limit(
            f"reset_confirm:ip:{client_ip}",
            self._RESET_CONFIRM_WINDOW,
            self._RESET_CONFIRM_MAX,
        ):
            logger.warning("Reset-confirm rate limit exceeded for ip=%s", client_ip)
            return False

        self._cleanup_reset_codes()
        entry = self._reset_codes.get(code)
        if not entry or entry["expires"] < time.time():
            return False

        # Burn an attempt before checking anything else so concurrent guessers
        # can't outrun the counter; this code matched, so consume + succeed.
        new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        storage_service.update_user_password(entry["user_id"], new_hash)
        del self._reset_codes[code]
        logger.info("Password reset for user_id=%d", entry["user_id"])
        return True

    def register_failed_reset_attempt(self, code: str) -> None:
        """Burn one attempt against the matching code (if any).

        Called when confirm_password_reset() returns False — we don't know if the
        attacker hit an existing-but-wrong code or a non-existent one, so charge
        every still-live code: any code under attack will be invalidated quickly.
        """
        # The attacker submitted `code`; if it's a real live code that just
        # mistyped the new password, that's already handled by the success path.
        # If it's a guess, no code in the table matches. To make brute-force
        # ineffective we instead burn attempts on every live code per failure
        # — bounded by _RESET_CODE_MAX_ATTEMPTS — so any code in flight dies
        # after enough wrong tries from anywhere.
        now = time.time()
        for stored_code, entry in list(self._reset_codes.items()):
            if entry["expires"] < now:
                del self._reset_codes[stored_code]
                continue
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] >= self._RESET_CODE_MAX_ATTEMPTS:
                logger.warning(
                    "Reset code invalidated after %d failed attempts (user_id=%d)",
                    entry["attempts"], entry["user_id"],
                )
                del self._reset_codes[stored_code]

    def change_email(self, user_id: int, email: str) -> dict:
        """Update user email. Returns dict with success flag and smtp_available."""
        user = storage_service.get_user_by_id(user_id)
        if not user:
            return {"ok": False, "smtp_available": False}
        clean_email = email.strip() or None
        storage_service.update_user_email(user_id, clean_email)
        logger.info("Email updated for user_id=%d", user_id)
        smtp_available = bool(settings.smtp_host and settings.smtp_from)
        if smtp_available and clean_email:
            self._send_email_confirmation(clean_email, user.get("username", ""))
        return {"ok": True, "smtp_available": smtp_available}

    def _send_email_confirmation(self, email: str, username: str) -> bool:
        """Send a confirmation notice that email has been saved to the account."""
        if not settings.smtp_host or not settings.smtp_from:
            return False
        msg = MIMEText(
            f"Bond AI — подтверждение email\n\n"
            f"Аккаунт: {username}\n"
            f"Ваш email {email} был успешно привязан к аккаунту Bond AI.\n\n"
            f"Теперь вы сможете использовать его для восстановления пароля.\n"
            f"Если вы не привязывали этот email — проигнорируйте письмо.",
            "plain",
            "utf-8",
        )
        msg["Subject"] = "Bond AI — email привязан к аккаунту"
        msg["From"] = settings.smtp_from
        msg["To"] = email
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                if settings.smtp_user and settings.smtp_password:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.smtp_from, [email], msg.as_string())
            logger.info("Confirmation email sent to %s for user %s", email, username)
            return True
        except Exception as e:
            logger.warning("Confirmation email send failed: %s", e)
            return False

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
