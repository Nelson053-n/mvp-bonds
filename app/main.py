from contextlib import asynccontextmanager
import logging
from pathlib import Path

import bcrypt
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.api.auth import router as auth_router
from app.api.portfolio import router as portfolio_router
from app.api.portfolios import router as portfolios_router
from app.api.settings import router as settings_router
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service
from app.services.portfolio_service import portfolio_service
from app.logging_config import setup_logging


setup_logging()
logger = logging.getLogger(__name__)


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
app.include_router(auth_router)
app.include_router(portfolios_router)
app.include_router(portfolio_router)
app.include_router(settings_router)

dashboard_path = Path(__file__).parent / "ui" / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(
        dashboard_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api-info")
async def api_info() -> dict[str, str]:
    return {
        "service": "MVP LLM Portfolio API",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/",
    }


@app.get("/share/{share_token}/table")
async def get_shared_portfolio_table(
    share_token: str,
    x_share_password: str | None = None,
) -> dict:
    """View a shared portfolio (public endpoint, no auth required)."""
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")

    # Check password if required
    if portfolio["share_password_hash"]:
        if not x_share_password:
            raise HTTPException(status_code=403, detail="Требуется пароль")
        if not bcrypt.checkpw(x_share_password.encode(), portfolio["share_password_hash"].encode()):
            raise HTTPException(status_code=403, detail="Неверный пароль")

    # Return table data
    rows = await portfolio_service.get_table(portfolio["id"])
    return {"items": rows}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
