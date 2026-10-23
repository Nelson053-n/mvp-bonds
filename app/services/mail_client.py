"""Ящик поддержки: чтение входящих по IMAP и отправка ответов по SMTP.

Письма хранятся в SQLite (mail_messages), админка читает только оттуда.
Входящие подтягивает фоновый цикл в leader-воркере; IMAP-сессия блокирующая,
поэтому всегда уходит в поток (иначе воркер замирает до таймаута сети).
Тело показывается только текстом: HTML-часть письма приводится к тексту
здесь же, в админке она не рендерится.
"""
from __future__ import annotations

import asyncio
import html
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parsedate_to_datetime

from app.config import settings
from app.services.mailer import SENDER_NAME
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_BODY_LIMIT = 20000
_FIRST_SYNC_LIMIT = 100   # при пустой базе тянем только последние N писем
_BATCH_LIMIT = 100
_SYNC_INTERVAL = 120
_KEY_VALIDITY = "mail_imap_uidvalidity"
_KEY_LAST_UID = "mail_imap_last_uid"

_sync_lock = asyncio.Lock()


def mail_configured() -> bool:
    return bool(settings.mail_user and settings.mail_password and settings.mail_from and settings.smtp_host)


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_message(raw: bytes) -> dict:
    """Письмо → поля для mail_messages."""
    msg = message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        try:
            body = part.get_content()
        except Exception:
            body = ""
        if part.get_content_type() == "text/html":
            body = _html_to_text(body)
    names = [a.get_filename() or "без имени" for a in msg.iter_attachments()]
    if names:
        body += "\n\n[Вложения не отображаются: " + ", ".join(names) + "]"
    try:
        dt = parsedate_to_datetime(str(msg["Date"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
    return {
        "msg_id": (str(msg["Message-ID"]).strip() if msg["Message-ID"] else None),
        "from_addr": str(msg["From"] or ""),
        "to_addr": str(msg["To"] or ""),
        "cc": str(msg["Cc"] or ""),
        "subject": str(msg["Subject"] or ""),
        "in_reply_to": (str(msg["In-Reply-To"]).strip() if msg["In-Reply-To"] else None),
        "body": body.strip()[:_BODY_LIMIT],
        "date": dt.astimezone(timezone.utc).isoformat(),
    }


def _sync_sync() -> int:
    """Подтянуть новые письма; возвращает число добавленных."""
    imap = imaplib.IMAP4_SSL(settings.mail_imap_host, settings.mail_imap_port,
                             ssl_context=ssl.create_default_context(), timeout=_TIMEOUT)
    added = 0
    try:
        imap.login(settings.mail_user, settings.mail_password)
        imap.select("INBOX", readonly=True)
        validity = ""
        typ, data = imap.response("UIDVALIDITY")
        if data and data[0]:
            validity = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
        last_uid = int(storage_service.get_setting(_KEY_LAST_UID, "0") or 0)
        if validity and storage_service.get_setting(_KEY_VALIDITY, "") != validity:
            # UID после смены UIDVALIDITY несравнимы со старыми; дубли отсекает msg_id
            last_uid = 0
            storage_service.set_setting(_KEY_VALIDITY, validity)
        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        new = [u for u in uids if u > last_uid]
        if last_uid == 0:
            new = new[-_FIRST_SYNC_LIMIT:]
        for uid in new[:_BATCH_LIMIT]:
            typ, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                m = parse_message(raw)
                if storage_service.add_mail("in", m.pop("msg_id") or f"imap-{uid}", **{
                        k: m[k] for k in ("from_addr", "to_addr", "subject", "body", "date", "cc", "in_reply_to")}):
                    added += 1
            last_uid = uid
            storage_service.set_setting(_KEY_LAST_UID, str(last_uid))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return added


async def sync_inbox() -> int:
    """Одна синхронизация; параллельные вызовы (цикл и кнопка) не пересекаются."""
    async with _sync_lock:
        return await asyncio.to_thread(_sync_sync)


def _send_sync(to: str, cc: str, subject: str, body: str, in_reply_to: str | None) -> str:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, settings.mail_from))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg_id = make_msgid(domain=settings.mail_from.rpartition("@")[2] or None)
    msg["Message-ID"] = msg_id
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    rcpt = [a.strip() for a in (to + "," + cc).split(",") if a.strip()]
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(settings.mail_user, settings.mail_password)
        smtp.sendmail(settings.mail_from, rcpt, msg.as_string())
    return msg_id


async def send_mail(to: str, subject: str, body: str, cc: str = "",
                    in_reply_to: str | None = None) -> int | None:
    """Отправить письмо от ящика поддержки и записать в «Исходящие». Возвращает id записи."""
    msg_id = await asyncio.to_thread(_send_sync, to, cc, subject, body, in_reply_to)
    logger.info("AUDIT support_mail_sent: to=%s", to)
    return storage_service.add_mail(
        "out", msg_id, settings.mail_from, to, subject, body,
        datetime.now(timezone.utc).isoformat(), cc=cc, in_reply_to=in_reply_to, is_read=True,
    )


async def mail_sync_loop() -> None:
    """Фоновый опрос ящика; без настройки MVP_MAIL_* молча ничего не делает."""
    await asyncio.sleep(60)
    while True:
        if mail_configured():
            try:
                n = await sync_inbox()
                if n:
                    logger.info("support mail: новых писем %d", n)
            except Exception as exc:
                logger.warning("support mail sync failed: %s %s", type(exc).__name__, exc)
        await asyncio.sleep(_SYNC_INTERVAL)
