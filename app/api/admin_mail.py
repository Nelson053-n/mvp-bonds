"""Встроенный почтовый клиент админки: ящик поддержки (входящие, исходящие, ответ)."""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_admin_user
from app.services import mail_client
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/mail", tags=["admin-mail"])

_ADDR_RE = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")
_SEND_LIMIT_PER_HOUR = 30


def _addresses(raw: str, field: str) -> str:
    """Список адресов через запятую; перевод строки в адресе — подмена заголовков."""
    items = [a.strip() for a in raw.split(",") if a.strip()]
    if not items or len(items) > 5 or not all(_ADDR_RE.match(a) for a in items):
        raise HTTPException(status_code=400, detail=f"Некорректный адрес в поле «{field}»")
    return ", ".join(items)


class SendPayload(BaseModel):
    to: str = Field(max_length=500)
    cc: str = Field(default="", max_length=500)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    reply_to_id: int | None = None


class ReadPayload(BaseModel):
    read: bool


@router.get("/status")
async def mail_status(admin: dict = Depends(get_admin_user)) -> dict:
    return {"configured": mail_client.mail_configured(), "address": mail_client.settings.mail_from,
            "unread": storage_service.count_unread_mail()}


@router.get("")
async def list_mail(direction: str = Query("in", pattern="^(in|out)$"),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    admin: dict = Depends(get_admin_user)) -> dict:
    return {"unread": storage_service.count_unread_mail(),
            "items": storage_service.list_mail(direction, limit, offset)}


@router.post("/sync")
async def sync_now(admin: dict = Depends(get_admin_user)) -> dict:
    if not mail_client.mail_configured():
        raise HTTPException(status_code=409, detail="Почтовый ящик поддержки не настроен")
    try:
        added = await mail_client.sync_inbox()
    except Exception as exc:
        logger.warning("support mail manual sync failed: %s %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="Не удалось связаться с почтовым сервером")
    return {"added": added, "unread": storage_service.count_unread_mail()}


@router.post("/send")
async def send(payload: SendPayload, admin: dict = Depends(get_admin_user)) -> dict:
    if not mail_client.mail_configured():
        raise HTTPException(status_code=409, detail="Почтовый ящик поддержки не настроен")
    to = _addresses(payload.to, "Кому")
    cc = _addresses(payload.cc, "Копия") if payload.cc.strip() else ""
    subject = payload.subject.strip()
    if "\r" in subject or "\n" in subject:
        raise HTTPException(status_code=400, detail="Тема не может содержать переводы строки")
    if not storage_service.check_rate_limit(f"admin_mail:{admin['sub']}", 3600, _SEND_LIMIT_PER_HOUR):
        raise HTTPException(status_code=429, detail="Слишком много писем за час")
    in_reply_to = None
    if payload.reply_to_id is not None:
        orig = storage_service.get_mail(payload.reply_to_id)
        if orig is None:
            raise HTTPException(status_code=404, detail="Исходное письмо не найдено")
        in_reply_to = orig["msg_id"]
    try:
        mail_id = await mail_client.send_mail(to, subject, payload.body, cc, in_reply_to)
    except Exception as exc:
        logger.warning("support mail send failed: %s %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="Письмо не отправлено: ошибка почтового сервера")
    return {"ok": True, "id": mail_id}


@router.get("/{mail_id}")
async def get_mail(mail_id: int, admin: dict = Depends(get_admin_user)) -> dict:
    m = storage_service.get_mail(mail_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Письмо не найдено")
    return m


@router.patch("/{mail_id}")
async def mark_read(mail_id: int, payload: ReadPayload, admin: dict = Depends(get_admin_user)) -> dict:
    if not storage_service.set_mail_read(mail_id, payload.read):
        raise HTTPException(status_code=404, detail="Письмо не найдено")
    return {"ok": True, "unread": storage_service.count_unread_mail()}
