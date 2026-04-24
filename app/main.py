from contextlib import asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.api.auth import router as auth_router
from app.api.bonds import router as bonds_router
from app.api.pdf import router as pdf_router
from app.api.portfolio import router as portfolio_router
from app.api.portfolios import router as portfolios_router
from app.api.settings import router as settings_router
from app.api.admin import router as admin_router
from app.api.tbank import router as tbank_router
from app.api.waitlist import router as waitlist_router
from app.api.watchlist import router as watchlist_router
from app.api.deps import get_shared_portfolio
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service
from app.services.portfolio_service import portfolio_service
from app.services.rating_utils import rating_worsened
from app.logging_config import setup_logging


setup_logging()
logger = logging.getLogger(__name__)

_ui_dir = Path(__file__).parent / "ui"
dashboard_path = _ui_dir / "dashboard.html"
landing_path = _ui_dir / "landing.html"
share_error_path = _ui_dir / "share_error.html"
not_found_path = _ui_dir / "404.html"

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
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://mc.yandex.ru https://mc.yandex.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://mc.yandex.ru https://mc.yandex.com; "
        "connect-src 'self' https://mc.yandex.ru https://mc.yandex.com; "
        "frame-ancestors 'none';"
    ),
}

# ── Static file serving config ──────────────────────────────────────────────
# Maps URL path → (filename, media_type, cache_header).
# Binary files use read_bytes(); text files use read_text().

_STATIC_CACHE = "public, max-age=86400"
_NO_CACHE = "no-cache, no-store"

_STATIC_FILES: dict[str, tuple[str, str, str]] = {
    "/manifest.json":       ("manifest.json",       "application/manifest+json", _STATIC_CACHE),
    "/og-image.png":        ("og-image.png",        "image/png",                 _STATIC_CACHE),
    "/icon-192.png":        ("icon-192.png",        "image/png",                 _STATIC_CACHE),
    "/icon-512.png":        ("icon-512.png",        "image/png",                 _STATIC_CACHE),
    "/favicon-32.png":      ("favicon-32.png",      "image/png",                 _STATIC_CACHE),
    "/favicon.ico":         ("favicon-32.png",      "image/png",                 _STATIC_CACHE),
    "/apple-touch-icon.png":("apple-touch-icon.png","image/png",                 _STATIC_CACHE),
    "/sw.js":               ("sw.js",               "application/javascript",    _NO_CACHE),
}

_TEXT_TYPES = {"application/manifest+json", "application/javascript"}


def _register_static_routes(application: FastAPI) -> None:
    """Register all static file routes from _STATIC_FILES config."""
    for url_path, (filename, media_type, cache) in _STATIC_FILES.items():
        file_path = _ui_dir / filename

        def _make_handler(fp: Path = file_path, mt: str = media_type, ch: str = cache):
            async def handler():
                content = fp.read_text(encoding="utf-8") if mt in _TEXT_TYPES else fp.read_bytes()
                return Response(content, media_type=mt, headers={"Cache-Control": ch})
            return handler

        application.get(url_path)(_make_handler())


# ── Background tasks ────────────────────────────────────────────────────────

async def _cleanup_shares_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            storage_service.cleanup_expired_shares()
        except Exception:
            pass


async def _snapshot_loop():
    """Save daily portfolio snapshots for all portfolios."""
    from datetime import datetime, timezone
    while True:
        now = datetime.now(timezone.utc)
        seconds_until_midnight = (24*3600) - (now.hour*3600 + now.minute*60 + now.second) + 300
        await asyncio.sleep(seconds_until_midnight % (24*3600) or 24*3600)
        try:
            portfolios_list = storage_service.get_all_portfolios_raw()
            for p in portfolios_list:
                try:
                    rows = await portfolio_service.get_table(p["id"])
                    total_value = sum(r.current_value or 0 for r in rows)
                    total_cost = sum((r.purchase_price or 0) * (r.quantity or 0) for r in rows)
                    storage_service.save_portfolio_snapshot(p["id"], total_value, total_cost)
                except Exception:
                    pass
        except Exception:
            pass


async def _notification_loop():
    """Check and send coupon notifications every 6 hours."""
    from app.services.notification_service import notification_service
    while True:
        await asyncio.sleep(3600 * 6)
        try:
            await notification_service.check_and_send_coupon_notifications()
        except Exception:
            pass


async def _tbank_sync_loop():
    """Sync all enabled T-Bank portfolios every 10 minutes."""
    SYNC_INTERVAL = 600
    await asyncio.sleep(SYNC_INTERVAL)  # first run 10 min after startup
    while True:
        try:
            from app.services.tbank_sync_service import do_sync_one
            syncs = storage_service.get_all_enabled_syncs()
            for cfg in syncs:
                try:
                    await do_sync_one(cfg["portfolio_id"], cfg)
                except Exception:
                    pass
        except Exception:
            logger.exception("tbank_sync_loop: unexpected error")
        await asyncio.sleep(SYNC_INTERVAL)


async def _rating_refresh_loop():
    """Refresh credit ratings for all portfolio tickers daily at 03:00 UTC."""
    from datetime import datetime, timezone
    from app.services.moex_service import moex_service
    from app.services.notification_service import notification_service

    while True:
        now = datetime.now(timezone.utc)
        secs_to_3am = ((3 - now.hour) % 24) * 3600 - now.minute * 60 - now.second
        if secs_to_3am <= 0:
            secs_to_3am += 86400
        await asyncio.sleep(secs_to_3am)
        try:
            items = storage_service.get_all_portfolio_items_for_rating()
            sem = asyncio.Semaphore(3)

            s = storage_service.get_all_settings()
            tg_token = s.get("tg_bot_token", "")
            tg_chat_id = s.get("tg_chat_id", "")

            async def _refresh_one(item):
                ticker = item["ticker"]
                async with sem:
                    try:
                        result = await moex_service.refresh_rating_with_sources(ticker)
                    except Exception as exc:
                        logger.warning("Rating refresh failed for %s: %s", ticker, exc)
                        return

                sl_rating = result["smartlab"]
                best_rating = result["best"]

                if sl_rating is not None:
                    storage_service.save_rating_history(ticker, sl_rating, "smartlab")

                    history = storage_service.get_recent_rating_history(ticker, "smartlab", limit=3)
                    if (
                        len(history) >= 3
                        and rating_worsened(history[2], history[1])
                        and rating_worsened(history[1], history[0])
                        and tg_token and tg_chat_id
                    ):
                        msg = (
                            f"\U0001f534 <b>Двойное ухудшение рейтинга</b>\n\n"
                            f"Бумага: <b>{ticker}</b>\n"
                            f"SmartLab: {history[2]} \u2192 {history[1]} \u2192 {history[0]}\n"
                            f"Рейтинг последовательно снижался дважды — возможный риск!"
                        )
                        await notification_service.send_telegram(tg_token, tg_chat_id, msg)
                        logger.warning("Double downgrade alert sent for %s", ticker)

                if best_rating is not None:
                    storage_service.update_rating_all_items_for_ticker(ticker, best_rating)

            await asyncio.gather(*(_refresh_one(i) for i in items))
            logger.info("Daily rating refresh: %d tickers processed", len(items))
        except Exception:
            logger.exception("Daily rating refresh failed")


def _backup_db_on_startup() -> None:
    """Create a rolling backup of the SQLite database on startup. Keeps 3 most recent."""
    import shutil
    from datetime import datetime, timezone

    db_path = Path(storage_service.db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"portfolio_{stamp}.db"
    try:
        shutil.copy2(db_path, dst)
        logger.info("DB backup created: %s", dst)
        backups = sorted(backup_dir.glob("portfolio_*.db"))
        for old in backups[:-3]:
            old.unlink()
            logger.info("Old backup removed: %s", old)
    except Exception:
        logger.exception("Failed to create DB backup")


# ── Application ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up application")
    _backup_db_on_startup()
    cache_service.start_background()
    asyncio.create_task(_cleanup_shares_loop())
    asyncio.create_task(_snapshot_loop())
    asyncio.create_task(_notification_loop())
    asyncio.create_task(_rating_refresh_loop())
    asyncio.create_task(_tbank_sync_loop())
    yield
    logger.info("Shutting down application")
    cache_service.stop_background()
    try:
        storage_service.checkpoint()
        logger.info("WAL checkpoint completed")
    except Exception:
        logger.exception("WAL checkpoint failed")


app = FastAPI(
    title="MVP LLM Portfolio",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Exception handlers ──────────────────────────────────────────────────────

_API_PREFIXES = (
    "/auth/", "/bonds/", "/portfolios/", "/pdf/",
    "/settings/", "/admin/", "/watchlist/", "/tbank/",
    "/health", "/api-info",
)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Return HTML 404 for browser requests, JSON 404 for API requests."""
    if request.url.path.startswith(_API_PREFIXES):
        return JSONResponse(status_code=404, content={"detail": getattr(exc, "detail", "Not found")})
    return HTMLResponse(not_found_path.read_text(encoding="utf-8"), status_code=404)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s %s — %s", request.method, request.url.path, exc, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Middleware ───────────────────────────────────────────────────────────────

@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


# ── Routers ─────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(bonds_router)
app.include_router(pdf_router)
app.include_router(portfolios_router)
app.include_router(portfolio_router)
app.include_router(settings_router)
app.include_router(admin_router)
app.include_router(tbank_router)
app.include_router(waitlist_router)
app.include_router(watchlist_router)

# Register all static file routes (favicon, icons, manifest, sw.js)
_register_static_routes(app)


# ── SEO / text routes ───────────────────────────────────────────────────────

@app.get("/robots.txt")
async def robots_txt():
    content = (
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: OAI-SearchBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: ChatGPT-User\n"
        "Allow: /\n"
        "\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: PerplexityBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "\n"
        "User-agent: Applebot-Extended\n"
        "Allow: /\n"
        "\n"
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app\n"
        "Disallow: /share/\n"
        "Disallow: /auth/\n"
        "Disallow: /portfolios/\n"
        "Disallow: /admin/\n"
        "Disallow: /settings/\n"
        "Disallow: /bonds/\n"
        "Disallow: /tbank/\n"
        "Disallow: /watchlist/\n"
        "Disallow: /pdf/\n"
        "\n"
        "Sitemap: https://bondai.ru/sitemap.xml\n"
    )
    return Response(content, media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        "    <loc>https://bondai.ru/</loc>\n"
        "    <lastmod>2026-04-25</lastmod>\n"
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/privacy</loc>\n"
        "    <lastmod>2026-03-13</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/terms</loc>\n"
        "    <lastmod>2026-03-13</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(content, media_type="application/xml")


@app.get("/llms.txt")
async def llms_txt():
    content = (
        "# Bond AI\n"
        "\n"
        "> Bond AI (bondai.ru) — AI-сервис для управления портфелем облигаций для российских розничных инвесторов. "
        "Отслеживает 1200+ облигаций MOEX в реальном времени с расчётом YTM, купонным календарём и AI-аналитикой "
        "на базе Anthropic Claude. Базовый тариф бесплатен навсегда.\n"
        "\n"
        "## Продукт\n"
        "\n"
        "- [Главная страница](https://bondai.ru/): Управление портфелем облигаций с AI-аналитикой и данными MOEX в реальном времени\n"
        "- [Политика конфиденциальности](https://bondai.ru/privacy): Обработка персональных данных по ФЗ-152\n"
        "- [Условия использования](https://bondai.ru/terms): Условия использования сервиса\n"
        "\n"
        "## Ключевые возможности\n"
        "\n"
        "- Данные MOEX ISS API в реальном времени (обновление каждые 15 минут)\n"
        "- AI-подбор портфеля по риск-профилю (от консервативного до агрессивного) на базе Claude\n"
        "- Купонный календарь с Telegram-уведомлениями о выплатах\n"
        "- Импорт портфеля из Т-Банк (Т-Инвестиции) по API\n"
        "- Мониторинг кредитных рейтингов (SmartLab + MOEX) с ежедневным обновлением\n"
        "- PDF-экспорт и совместные ссылки на портфель с защитой паролем\n"
        "- Список наблюдения с ценовыми алертами\n"
        "\n"
        "## Контакт\n"
        "\n"
        "- Email: support@bondai.ru\n"
    )
    return Response(content, media_type="text/plain; charset=utf-8")


# ── HTML pages ──────────────────────────────────────────────────────────────

_HTML_PAGES: dict[str, Path] = {
    "/":        landing_path,
    "/landing": landing_path,
    "/privacy": _ui_dir / "privacy.html",
    "/terms":   _ui_dir / "terms.html",
    "/app":     dashboard_path,
}

for _page_url, _page_path in _HTML_PAGES.items():
    def _make_page_handler(fp: Path = _page_path):
        async def handler() -> HTMLResponse:
            return HTMLResponse(fp.read_text(encoding="utf-8"), headers=_NO_CACHE_HEADERS)
        return handler
    app.get(_page_url, response_class=HTMLResponse)(_make_page_handler())


# ── Share endpoints ─────────────────────────────────────────────────────────

@app.get("/share/{share_token}", response_class=HTMLResponse)
async def view_shared_portfolio(share_token: str) -> HTMLResponse:
    """View shared portfolio page (public endpoint, no auth required)."""
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        return HTMLResponse(
            share_error_path.read_text(encoding="utf-8"),
            status_code=404,
        )

    html = dashboard_path.read_text(encoding="utf-8")
    html = html.replace(
        "<!-- __SHARE_INJECT__ -->",
        f"<script>window.shareToken={json.dumps(share_token)};window.isSharedView=true;</script>",
        1,
    )
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)


@app.get("/share/{share_token}/table")
async def get_shared_portfolio_table(
    portfolio: dict = Depends(get_shared_portfolio),
) -> dict:
    """Shared portfolio table (public, no auth)."""
    rows = await portfolio_service.get_table(portfolio["id"])
    return {"items": rows}


@app.get("/share/{share_token}/snapshots")
async def get_shared_portfolio_snapshots(
    days: int = 90,
    portfolio: dict = Depends(get_shared_portfolio),
) -> list[dict]:
    """Portfolio value history for shared view (public, no auth)."""
    if days not in (7, 30, 90, 365):
        days = 90
    return storage_service.get_portfolio_snapshots(portfolio["id"], days)


# ── Utility endpoints ───────────────────────────────────────────────────────

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
