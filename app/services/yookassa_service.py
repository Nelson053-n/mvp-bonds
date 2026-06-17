"""YooKassa (ЮKassa) client for one-off Pro payments.

One-off model: user picks a plan (month/year), we create a YooKassa payment
with a redirect confirmation, the user pays, YooKassa calls our webhook, and we
grant Pro for the plan's days. No recurring/auto-renew, no 54-FZ receipt (yet).

Disabled unless MVP_YOOKASSA_SHOP_ID and MVP_YOOKASSA_SECRET_KEY are set.
Auth is HTTP Basic (shop_id : secret_key). Each create needs a unique
Idempotence-Key so retries don't double-charge.
"""
from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_API = "https://api.yookassa.ru/v3/payments"
_RETURN_URL = "https://bondai.ru/app?pro=success"

# plan -> (amount_rub, pro_days)
PLANS = {
    "month": (settings.pro_price_month, settings.pro_days_month),
    "year": (settings.pro_price_year, settings.pro_days_year),
}


def enabled() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


def _auth_header() -> str:
    raw = f"{settings.yookassa_shop_id}:{settings.yookassa_secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def create_payment(user_id: int, plan: str, idempotence_key: str) -> dict | None:
    """Create a YooKassa payment. Returns {id, confirmation_url} or None on failure.

    `metadata.user_id` + `metadata.plan` come back in the webhook so we know
    whom to grant Pro to and for how long.
    """
    if not enabled() or plan not in PLANS:
        return None
    amount, _days = PLANS[plan]
    body = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": _RETURN_URL},
        "capture": True,
        "description": f"Bond AI Pro — {'месяц' if plan == 'month' else 'год'}",
        "metadata": {"user_id": str(user_id), "plan": plan},
    }
    headers = {
        "Authorization": _auth_header(),
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(_API, json=body, headers=headers)
        if resp.status_code not in (200, 201):
            logger.warning("YooKassa create failed HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "confirmation_url": (data.get("confirmation") or {}).get("confirmation_url"),
        }
    except Exception as exc:  # noqa: BLE001 - never propagate to the request
        logger.warning("YooKassa create error: %s", exc)
        return None


async def get_payment(payment_id: str) -> dict | None:
    """Fetch a payment to verify its real status (webhooks are untrusted input)."""
    if not enabled() or not payment_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_API}/{payment_id}", headers={"Authorization": _auth_header()})
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("YooKassa get error: %s", exc)
        return None
