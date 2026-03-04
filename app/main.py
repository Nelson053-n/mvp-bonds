from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.portfolio import router as portfolio_router
from app.services.cache_service import cache_service
from app.logging_config import setup_logging


setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: warm cache and start background refresh
    logger.info("Starting up application")
    try:
        await cache_service.refresh()
        logger.info("Cache warmed successfully")
    except Exception as exc:
        logger.warning("Failed to warm cache on startup: %s", exc)
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
app.include_router(portfolio_router)

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
