"""Обращения пользователей в техподдержку: форма для пользователя + уведомление админу в Telegram."""
import html
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.services.notification_service import notification_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["support"])

MAX_MESSAGE_LEN = 2000
_TG_PREVIEW_LEN = 1500


class SupportRequest(BaseModel):
    message: str = Field(min_length=5, max_length=MAX_MESSAGE_LEN)


async def notify_admin_new_ticket(ticket_id: int, user: dict, message: str) -> bool:
    """Telegram админу (токен и chat_id — в app_settings, как у остальных алертов)."""
    s = storage_service.get_all_settings()
    token, chat_id = s.get("tg_bot_token", ""), s.get("tg_chat_id", "")
    if not token or not chat_id:
        logger.warning("support ticket %d: admin Telegram не настроен, уведомление не отправлено", ticket_id)
        return False
    contacts = []
    if user.get("email"):
        contacts.append(html.escape(user["email"]))
    if user.get("tg_chat_id"):
        contacts.append("Telegram привязан")
    text = (
        f"🆘 <b>Обращение в поддержку #{ticket_id}</b>\n"
        f"От: {html.escape(user['username'])} (id {user['id']})"
        + (f"\nСвязь: {', '.join(contacts)}" if contacts else "")
        + f"\n\n{html.escape(message[:_TG_PREVIEW_LEN])}"
        + ("…" if len(message) > _TG_PREVIEW_LEN else "")
    )
    return await notification_service.send_telegram(token, chat_id, text)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(payload: SupportRequest, current_user: dict = Depends(get_current_user)) -> dict:
    user = storage_service.get_user_by_id(current_user["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="Неверный или истекший токен")
    message = payload.message.strip()
    if len(message) < 5:
        raise HTTPException(status_code=422, detail="Опишите вопрос подробнее")
    if not storage_service.check_rate_limit(f"support:{user['id']}", 3600, 5):
        raise HTTPException(status_code=429, detail="Слишком много обращений. Попробуйте через час.")
    ticket_id = storage_service.create_support_ticket(user["id"], message)
    try:
        await notify_admin_new_ticket(ticket_id, user, message)
    except Exception:
        logger.exception("support ticket %d: уведомление админу не ушло", ticket_id)
    return {"ok": True, "id": ticket_id}
