from contextlib import asynccontextmanager
import logging
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

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
app.include_router(bonds_router)
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
    <title>Портфель не найден — Bond AI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
    <style>
        *, *::before, *::after { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: 'Inter', system-ui, sans-serif;
            background: #0f172a;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        nav {
            padding: 14px 32px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: 1px solid #1e293b;
        }
        .brand-mark {
            width: 30px; height: 30px;
            background: #2563eb; border-radius: 7px;
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 15px; color: #fff;
        }
        .brand-name { font-weight: 700; font-size: 14px; color: #f1f5f9; letter-spacing: -.2px; }
        .brand-badge {
            background: #1e3a8a; color: #93c5fd;
            font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px;
        }
        main {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 60px 24px;
        }
        .card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 48px 40px;
            text-align: center;
            max-width: 480px;
            width: 100%;
            box-shadow: 0 25px 50px rgba(0,0,0,.5);
        }
        .card-icon {
            width: 64px; height: 64px;
            background: #1e3a8a; border-radius: 16px;
            display: flex; align-items: center; justify-content: center;
            font-size: 28px; margin: 0 auto 24px;
        }
        h1 {
            margin: 0 0 12px;
            font-size: 22px; font-weight: 700;
            color: #f1f5f9; letter-spacing: -.3px;
        }
        p {
            margin: 0 0 10px;
            font-size: 14px; color: #94a3b8; line-height: 1.65;
        }
        .back-link {
            display: inline-block;
            margin-top: 20px;
            padding: 10px 24px;
            background: #2563eb; color: #fff;
            text-decoration: none; border-radius: 8px;
            font-size: 14px; font-weight: 600;
            transition: background .15s;
        }
        .back-link:hover { background: #1d4ed8; }
        footer {
            border-top: 1px solid #1e293b;
            padding: 16px 32px;
            display: flex; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 8px;
        }
        footer span { font-size: 11px; color: #475569; }
        footer a { color: #3b82f6; text-decoration: none; }
    </style>
</head>
<body>
    <nav>
        <div class="brand-mark">B</div>
        <span class="brand-name">Bond AI</span>
    </nav>
    <main>
        <div class="card">
            <div class="card-icon">🔗</div>
            <h1>Ссылка истекла или неверна</h1>
            <p>Портфель по этой ссылке больше не доступен.<br>Владелец мог удалить его или отозвать общий доступ.</p>
            <p style="font-size: 12px; color: #475569;">Если вы считаете, что это ошибка — обратитесь к владельцу портфеля.</p>
            <a href="/" class="back-link">← На главную</a>
        </div>
    </main>
    <footer>
        <span>© Bond AI · Не является инвестиционной рекомендацией</span>
        <span>Откройте счёт: <a href="https://www.tinkoff.ru/invest/" target="_blank" rel="noopener">Т‑Инвестиции</a></span>
    </footer>
</body>
</html>"""
        return HTMLResponse(error_html, status_code=404)

    # Return the dashboard HTML with share token embedded
    html = dashboard_path.read_text(encoding="utf-8")
    # Inject the share token into the HTML so JS knows to load this shared portfolio
    html = html.replace(
        "<!-- __SHARE_INJECT__ -->",
        f"<script>window.shareToken='{share_token}';window.isSharedView=true;</script>",
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
