"""Pro billing via YooKassa (one-off month/year payments)."""
import ipaddress
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.config import settings
from app.services import yookassa_service
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Official YooKassa notification source ranges (docs → "IP-адреса ЮKassa").
# Webhooks arriving from outside these are rejected before any outbound call.
_YOOKASSA_NETWORKS = [
    ipaddress.ip_network(n) for n in (
        "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25",
        "77.75.156.11/32", "77.75.156.35/32", "77.75.154.128/25",
        "2a02:5180::/32",
    )
]

# YooKassa payment ids are UUIDs with a couple of dashes turned into dots in
# some SDKs, but the canonical form is a plain UUID. Reject anything else early
# so a forged id never triggers an outbound get_payment lookup.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _ip_allowed(client_ip: str) -> bool:
    """True if the request comes from an official YooKassa subnet."""
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in _YOOKASSA_NETWORKS)


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
    verify its real status before granting Pro (never trust the webhook body).

    Hardening (DoS-amplification): only official YooKassa IPs may reach the
    outbound get_payment lookup, per-IP rate-limited, and a forged id that
    doesn't look like a YooKassa UUID is rejected before any external call."""
    client_ip = request.client.host if request.client else ""

    # (1) IP-allowlist — official YooKassa subnets only (toggleable for dev).
    if settings.yookassa_webhook_ip_check and not _ip_allowed(client_ip):
        logger.warning("billing_webhook rejected: untrusted ip=%s", client_ip)
        raise HTTPException(status_code=403, detail="forbidden")

    # (2) Per-IP rate limit (60/hour) to cap load on us and the YooKassa API.
    if client_ip and not storage_service.check_rate_limit(
        f"billing_webhook:ip:{client_ip}", 3600, 60
    ):
        logger.warning("billing_webhook rate limit exceeded ip=%s", client_ip)
        raise HTTPException(status_code=429, detail="too many requests")

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}  # always 200 so YooKassa doesn't retry-storm
    obj = (body or {}).get("object") or {}
    payment_id = obj.get("id")
    # (3) Bail out before any outbound call if the id isn't a YooKassa UUID.
    if not payment_id or not _UUID_RE.match(str(payment_id)):
        return {"ok": True}
    verified = await yookassa_service.get_payment(payment_id)
    if verified:
        _grant_pro_from_payment(verified)
    return {"ok": True}
