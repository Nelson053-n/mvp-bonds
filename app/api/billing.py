"""Pro billing via YooKassa (one-off month/year payments)."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.config import settings
from app.services import yookassa_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/config")
async def billing_config() -> dict:
    """Public: prices + whether billing is enabled (UI hides buttons if not)."""
    return {
        "enabled": yookassa_service.enabled(),
        "price_month": settings.pro_price_month,
        "price_year": settings.pro_price_year,
    }


class CreatePaymentInput(BaseModel):
    plan: str  # "month" | "year"


@router.post("/create")
async def billing_create(
    payload: CreatePaymentInput,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a YooKassa payment and return the confirmation URL to redirect to."""
    if not yookassa_service.enabled():
        raise HTTPException(status_code=503, detail="Оплата временно недоступна")
    if payload.plan not in yookassa_service.PLANS:
        raise HTTPException(status_code=400, detail="Неизвестный тариф")
    result = await yookassa_service.create_payment(
        current_user["sub"], payload.plan, idempotence_key=str(uuid.uuid4())
    )
    if not result or not result.get("confirmation_url"):
        raise HTTPException(status_code=502, detail="Не удалось создать платёж")
    logger.info("AUDIT billing_create: user_id=%s plan=%s payment_id=%s",
                current_user["sub"], payload.plan, result.get("id"))
    return {"confirmation_url": result["confirmation_url"], "payment_id": result["id"]}


def _grant_pro_from_payment(payment: dict) -> bool:
    """Grant Pro to the user named in a *verified* succeeded payment. Idempotent-ish."""
    if not payment or payment.get("status") != "succeeded":
        return False
    meta = payment.get("metadata") or {}
    try:
        user_id = int(meta.get("user_id"))
    except (TypeError, ValueError):
        return False
    plan = meta.get("plan")
    plan_info = yookassa_service.PLANS.get(plan)
    if not plan_info:
        return False
    amount, days = plan_info
    # Extend from the later of now / current expiry so stacking a renewal adds time.
    from datetime import date, timedelta
    user = storage_service.get_user_by_id(user_id)
    base = date.today()
    if user and user.get("pro_until"):
        try:
            cur = date.fromisoformat(str(user["pro_until"])[:10])
            if cur > base:
                base = cur
        except ValueError:
            pass
    until = (base + timedelta(days=days)).isoformat()
    storage_service.set_user_pro(user_id, True, until)
    # Record the payment for NPD bookkeeping (receipts entered into "Мой налог"
    # by hand — YooKassa has no auto-integration for that).
    try:
        storage_service.record_pro_payment(
            payment_id=str(payment.get("id") or ""),
            user_id=user_id,
            username=(user or {}).get("username", ""),
            plan=plan, amount=float(amount), status="succeeded",
        )
    except Exception:
        logger.exception("record_pro_payment failed")
    logger.info("AUDIT pro_granted_via_payment: user_id=%d plan=%s until=%s", user_id, plan, until)
    return True


@router.post("/webhook")
async def billing_webhook(request: Request) -> dict:
    """YooKassa notification. Untrusted input — we RE-FETCH the payment by id and
    verify its real status before granting Pro (never trust the webhook body)."""
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}  # always 200 so YooKassa doesn't retry-storm
    obj = (body or {}).get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return {"ok": True}
    verified = await yookassa_service.get_payment(payment_id)
    if verified:
        _grant_pro_from_payment(verified)
    return {"ok": True}
