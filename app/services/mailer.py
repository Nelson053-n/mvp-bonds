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
from email.utils import formatdate, make_msgid

from app.config import settings

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT = 15


def smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to
    # Gmail отклоняет письма без Message-ID: 550 5.7.1 RFC 5322.
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.rpartition("@")[2] or None)
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT) as smtp:
        smtp.ehlo()
        # Без context smtplib берёт непроверяющий контекст (CERT_NONE): любой
        # на пути до SMTP-сервера получил бы пароль ящика и коды сброса
        # (аудит 12.10.2026, M1). Сертификат Beget *.beget.com проверку проходит.
        smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.smtp_from, [to], msg.as_string())


async def send_mail(to: str, subject: str, body: str, what: str) -> bool:
    """Письмо в отдельном потоке; False — не настроено или не ушло (в лог)."""
    if not smtp_configured():
        logger.warning("mail %s skipped: MVP_SMTP_* не заданы", what)
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
    except Exception as exc:
        logger.warning("mail %s failed: %s %s", what, type(exc).__name__, exc)
        return False
    logger.info("AUDIT mail_sent: %s", what)
    return True
