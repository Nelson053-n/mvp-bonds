from contextlib import asynccontextmanager
import logging
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from app.api.auth import router as auth_router
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
app.include_router(admin_router)

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


@app.get("/share/{share_token}", response_class=HTMLResponse)
async def view_shared_portfolio(share_token: str) -> HTMLResponse:
    """View shared portfolio page (public endpoint, no auth required)."""
    # Check if portfolio with this share token exists
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        error_html = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Портфель не найден</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 20px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container {
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            padding: 60px 40px;
            text-align: center;
            max-width: 500px;
        }
        .icon {
            font-size: 64px;
            margin-bottom: 20px;
        }
        h1 {
            margin: 0 0 16px;
            font-size: 28px;
            color: #1e293b;
        }
        p {
            margin: 0 0 12px;
            font-size: 16px;
            color: #64748b;
            line-height: 1.6;
        }
        .details {
            background: #f1f5f9;
            border-left: 4px solid #ef4444;
            padding: 12px 16px;
            border-radius: 6px;
            margin: 24px 0;
            text-align: left;
            font-size: 14px;
            color: #475569;
            font-family: 'Courier New', monospace;
            word-break: break-all;
        }
        .back-link {
            display: inline-block;
            margin-top: 24px;
            padding: 10px 20px;
            background: #3b82f6;
            color: white;
            text-decoration: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }
        .back-link:hover {
            background: #2563eb;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">🔗</div>
        <h1>Ссылка истекла или неверна</h1>
        <p>Портфель по этой ссылке больше не доступен. Возможно, владелец удалил портфель или отозвал доступ.</p>
        <div class="details">
            Share token: {share_token}
        </div>
        <p style="font-size: 13px; color: #94a3b8;">Если вы считаете, что это ошибка, обратитесь к владельцу портфеля.</p>
        <a href="/" class="back-link">← На главную</a>
    </div>
</body>
</html>"""
        return HTMLResponse(error_html, status_code=404)

    # Return the dashboard HTML with share token embedded
    html = dashboard_path.read_text(encoding="utf-8")
    # Inject the share token into the HTML so JS knows to load this shared portfolio
    html = html.replace(
        "<script>",
        f"<script>window.shareToken='{share_token}';window.isSharedView=true;</script><script>",
        1
    )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/share/{share_token}/table")
async def get_shared_portfolio_table(
    share_token: str,
    x_share_password: str | None = Header(None),
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
