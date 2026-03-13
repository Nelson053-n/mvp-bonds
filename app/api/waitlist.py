"""Waitlist router for Pro plan email collection."""

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.storage_service import storage_service

router = APIRouter()

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


@router.post("/waitlist")
async def join_waitlist(request: Request):
    """Public endpoint: add email to Pro waitlist."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Неверный формат запроса"})

    email = (body.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        return JSONResponse(status_code=422, content={"detail": "Укажите корректный email"})
    if len(email) > 254:
        return JSONResponse(status_code=422, content={"detail": "Email слишком длинный"})

    try:
        storage_service.add_waitlist_email(email)
    except Exception:
        return JSONResponse(status_code=200, content={"ok": True, "message": "Вы уже в списке ожидания!"})

    return JSONResponse(status_code=201, content={"ok": True, "message": "Вы в списке ожидания!"})
