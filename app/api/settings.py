from fastapi import APIRouter
from pydantic import BaseModel

from app.services.storage_service import storage_service
from app.services.notification_service import notification_service

router = APIRouter(prefix="/settings", tags=["settings"])


class NotificationSettings(BaseModel):
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    price_drop_threshold: float = 5.0


@router.get("/notifications", response_model=NotificationSettings)
async def get_notifications() -> NotificationSettings:
    s = storage_service.get_all_settings()
    return NotificationSettings(
        tg_bot_token=s.get("tg_bot_token", ""),
        tg_chat_id=s.get("tg_chat_id", ""),
        price_drop_threshold=float(s.get("price_drop_threshold", "5.0")),
    )


@router.post("/notifications", response_model=NotificationSettings)
async def save_notifications(
    payload: NotificationSettings,
) -> NotificationSettings:
    storage_service.set_setting("tg_bot_token", payload.tg_bot_token)
    storage_service.set_setting("tg_chat_id", payload.tg_chat_id)
    storage_service.set_setting(
        "price_drop_threshold", str(payload.price_drop_threshold)
    )
    return payload


@router.post("/notifications/test")
async def test_notification(payload: NotificationSettings) -> dict[str, bool]:
    ok = await notification_service.send_telegram(
        payload.tg_bot_token,
        payload.tg_chat_id,
        "✅ Тестовое уведомление от MVP Bonds Portfolio",
    )
    return {"success": ok}
