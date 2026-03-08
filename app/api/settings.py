from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_admin_user
from app.services.storage_service import storage_service
from app.services.notification_service import notification_service

router = APIRouter(prefix="/settings", tags=["settings"])


class NotificationSettings(BaseModel):
    tg_bot_token: str = Field(default="", max_length=200)
    tg_chat_id: str = Field(default="", max_length=64)
    price_drop_threshold: float = Field(default=5.0, ge=0.1, le=100.0)
    tg_lang: str = Field(default="ru", pattern="^(ru|en)$")


class PersonalNotificationsInput(BaseModel):
    coupon_notif_enabled: bool
    coupon_notif_days: int = Field(..., ge=1, le=30)


@router.get("/notifications", response_model=NotificationSettings)
async def get_notifications(admin: dict = Depends(get_admin_user)) -> NotificationSettings:
    s = storage_service.get_all_settings()
    return NotificationSettings(
        tg_bot_token=s.get("tg_bot_token", ""),
        tg_chat_id=s.get("tg_chat_id", ""),
        price_drop_threshold=float(s.get("price_drop_threshold", "5.0")),
        tg_lang=s.get("tg_lang", "ru"),
    )


@router.post("/notifications", response_model=NotificationSettings)
async def save_notifications(
    payload: NotificationSettings,
    admin: dict = Depends(get_admin_user),
) -> NotificationSettings:
    storage_service.set_setting("tg_bot_token", payload.tg_bot_token)
    storage_service.set_setting("tg_chat_id", payload.tg_chat_id)
    storage_service.set_setting("price_drop_threshold", str(payload.price_drop_threshold))
    storage_service.set_setting("tg_lang", payload.tg_lang or "ru")
    return payload


@router.post("/notifications/test")
async def test_notification(
    payload: NotificationSettings,
    admin: dict = Depends(get_admin_user),
) -> dict[str, bool]:
    msg = (
        "✅ Test notification from Bond AI"
        if payload.tg_lang == "en"
        else "✅ Тестовое уведомление от Bond AI"
    )
    ok = await notification_service.send_telegram(
        payload.tg_bot_token,
        payload.tg_chat_id,
        msg,
    )
    return {"success": ok}


@router.get("/notifications/personal")
async def get_personal_notifications(
    current_user: dict = Depends(get_current_user),
) -> dict:
    return storage_service.get_user_notification_settings(current_user["sub"])


@router.post("/notifications/personal")
async def update_personal_notifications(
    payload: PersonalNotificationsInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    storage_service.update_user_notification_settings(
        current_user["sub"], payload.coupon_notif_enabled, payload.coupon_notif_days
    )
    return {"ok": True}
