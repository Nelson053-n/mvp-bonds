"""Отправка писем через SMTP (MVP_SMTP_*).

Раньше одна и та же сборка письма и SMTP-сессия жили в трёх местах
(сброс пароля, подтверждение email, алерт о недоступности Telegram), и
правку Message-ID пришлось вносить трижды. Двое из трёх вызывали
блокирующий smtplib прямо из async-кода — воркер замирал на всю SMTP-сессию
(до таймаута при недоступном сервере).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from app.config import settings

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT = 15
SENDER_NAME = "BondAI"


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def sender_credentials(sender: str) -> tuple[str, str, str] | None:
    """(логин, пароль, адрес) для ящика-отправителя.

    alert   — системные письма: МVP_SMTP_* (alert@bondai.ru).
    support — ответы поддержки и письма восстановления пароля: MVP_MAIL_*
              (support@bondai.ru). Beget требует, чтобы From совпадал с
              аутентифицированным ящиком, поэтому учётки разные.
    """
    if sender == "support" and settings.mail_user and settings.mail_password and settings.mail_from:
        return settings.mail_user, settings.mail_password, settings.mail_from
    # Запас: ящик поддержки не настроен — письмо всё равно уходит, но с alert.
    # Молча терять коды сброса пароля нельзя.
    if settings.smtp_from:
        return settings.smtp_user, settings.smtp_password, settings.smtp_from
    return None


def _send_sync(to: str, subject: str, body: str, sender: str = "alert") -> None:
    creds = sender_credentials(sender) or ("", "", settings.smtp_from)
    user, password, address = creds
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, address))
    msg["To"] = to
    # Gmail отклоняет письма без Message-ID: 550 5.7.1 RFC 5322.
    msg["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT) as smtp:
        smtp.ehlo()
        # Без context smtplib берёт непроверяющий контекст (CERT_NONE): любой
        # на пути до SMTP-сервера получил бы пароль ящика и коды сброса
        # (аудит 12.10.2026, M1). Сертификат Beget *.beget.com проверку проходит.
        smtp.starttls(context=ssl.create_default_context())
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(address, [to], msg.as_string())


async def send_mail(to: str, subject: str, body: str, what: str,
                    sender: str = "alert") -> bool:
    """Письмо в отдельном потоке; False — не настроено или не ушло (в лог)."""
    if not smtp_configured() or sender_credentials(sender) is None:
        logger.warning("mail %s skipped: ящик %s не настроен", what, sender)
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body, sender)
    except Exception as exc:
        logger.warning("mail %s failed: %s %s", what, type(exc).__name__, exc)
        return False
    logger.info("AUDIT mail_sent: %s", what)
    return True
