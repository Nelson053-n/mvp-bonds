from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.api.auth import router as auth_router
from app.api.bonds import router as bonds_router
from app.api.portfolio import router as portfolio_router
from app.api.portfolios import router as portfolios_router
from app.api.settings import router as settings_router
from app.api.admin import router as admin_router
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service
from app.services.portfolio_service import portfolio_service
from app.logging_config import setup_logging


setup_logging()
logger = logging.getLogger(__name__)

_ui_dir = Path(__file__).parent / "ui"
dashboard_path = _ui_dir / "dashboard.html"
share_error_path = _ui_dir / "share_error.html"

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: start background refresh (portfolios are lazy-loaded)
    logger.info("Starting up application")
    cache_service.start_background()
    yield
    # Shutdown
    logger.info("Shutting down application")
    cache_service.stop_background()


app = FastAPI(
    title="MVP LLM Portfolio",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


app.include_router(auth_router)
app.include_router(bonds_router)
app.include_router(portfolios_router)
app.include_router(portfolio_router)
app.include_router(settings_router)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(
        dashboard_path.read_text(encoding="utf-8"),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/api-info")
async def api_info() -> dict[str, str]:
    return {
        "service": "MVP LLM Portfolio API",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/",
    }


@app.get("/share/{share_token}", response_class=HTMLResponse)
async def view_shared_portfolio(share_token: str) -> HTMLResponse:
    """View shared portfolio page (public endpoint, no auth required)."""
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        return HTMLResponse(
            share_error_path.read_text(encoding="utf-8"),
            status_code=404,
        )

    # Inject share token via json.dumps to prevent XSS
    html = dashboard_path.read_text(encoding="utf-8")
    html = html.replace(
        "<!-- __SHARE_INJECT__ -->",
        f"<script>window.shareToken={json.dumps(share_token)};window.isSharedView=true;</script>",
        1,
    )
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)


@app.get("/share/{share_token}/table")
async def get_shared_portfolio_table(
    share_token: str,
    x_share_password: str | None = Header(None),
) -> dict:
    """View a shared portfolio (public endpoint, no auth required)."""
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")

    if portfolio["share_password_hash"]:
        if not x_share_password:
            raise HTTPException(status_code=403, detail="Требуется пароль")
        if not bcrypt.checkpw(x_share_password.encode(), portfolio["share_password_hash"].encode()):
            raise HTTPException(status_code=403, detail="Неверный пароль")

    rows = await portfolio_service.get_table(portfolio["id"])
    return {"items": rows}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
