"""
YooKassa payments stub.
Real payment processing is not implemented — structure is ready for integration.
"""

import time

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/checkout")
async def create_checkout(
    plan: str = Query("pro"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Create a payment session (stub). Returns a stub response with admin contact info."""
    return {
        "payment_id": f"stub_{current_user['sub']}_{int(time.time())}",
        "confirmation_url": None,
        "status": "stub",
        "message": (
            "Оплата временно недоступна. "
            "Обратитесь к администратору для активации Pro-тарифа."
        ),
    }


@router.post("/webhook")
async def yookassa_webhook(request: Request) -> dict:
    """YooKassa webhook endpoint (stub — logs and acknowledges)."""
    return {"ok": True}
