from contextlib import asynccontextmanager
import asyncio
import json
import logging
from pathlib import Path
import time

import bcrypt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.api.auth import router as auth_router
from app.api.bonds import router as bonds_router
from app.api.pdf import router as pdf_router
from app.api.portfolio import router as portfolio_router
from app.api.portfolios import router as portfolios_router
from app.api.settings import router as settings_router
from app.api.admin import router as admin_router
from app.api.tbank import router as tbank_router
from app.api.watchlist import router as watchlist_router
from app.services.cache_service import cache_service
from app.services.storage_service import storage_service
from app.services.portfolio_service import portfolio_service
from app.logging_config import setup_logging


setup_logging()
logger = logging.getLogger(__name__)

_ui_dir = Path(__file__).parent / "ui"
dashboard_path = _ui_dir / "dashboard.html"
landing_path = _ui_dir / "landing.html"
share_error_path = _ui_dir / "share_error.html"
privacy_path = _ui_dir / "privacy.html"
terms_path = _ui_dir / "terms.html"

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
        # Run once per day at ~00:05 UTC
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


_RATING_ORDER = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]


def _rating_rank(r: str | None) -> int:
    """Lower rank = better rating. None → 999 (unknown)."""
    if r is None:
        return 999
    return _RATING_ORDER.index(r) if r in _RATING_ORDER else 998


def _rating_worsened(prev: str | None, curr: str | None) -> bool:
    """Return True if curr is strictly worse than prev."""
    return _rating_rank(curr) > _rating_rank(prev)


async def _rating_refresh_loop():
    """Refresh credit ratings for all portfolio tickers daily at 03:00 UTC.

    Logic per ticker:
    - Fetch SmartLab and MOEX ratings independently.
    - If SmartLab rating differs from MOEX → use SmartLab (more up-to-date), save to history.
    - If SmartLab rating worsened twice in a row → send Telegram alert.
    - Update portfolio_items with the best available rating.
    """
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

            # Load TG settings once
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
                moex_rating = result["moex"]
                best_rating = result["best"]

                # Save SmartLab rating to history if available
                if sl_rating is not None:
                    storage_service.save_rating_history(ticker, sl_rating, "smartlab")

                    # Check double downgrade on SmartLab
                    history = storage_service.get_recent_rating_history(ticker, "smartlab", limit=3)
                    # history[0] = newest (current), history[1] = previous, history[2] = one before
                    if (
                        len(history) >= 3
                        and _rating_worsened(history[2], history[1])
                        and _rating_worsened(history[1], history[0])
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

                # Update DB if we got a best rating
                if best_rating is not None:
                    storage_service.update_rating_all_items_for_ticker(ticker, best_rating)

            await asyncio.gather(*(_refresh_one(i) for i in items))
            logger.info("Daily rating refresh: %d tickers processed", len(items))
        except Exception:
            logger.exception("Daily rating refresh failed")


def _backup_db_on_startup() -> None:
    """Create a rolling backup of the SQLite database on startup.

    Keeps up to 3 dated backups in data/backups/.
    """
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
        # Keep only the 3 most recent backups
        backups = sorted(backup_dir.glob("portfolio_*.db"))
        for old in backups[:-3]:
            old.unlink()
            logger.info("Old backup removed: %s", old)
    except Exception:
        logger.exception("Failed to create DB backup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create DB backup, then start background tasks
    logger.info("Starting up application")
    _backup_db_on_startup()
    cache_service.start_background()
    asyncio.create_task(_cleanup_shares_loop())
    asyncio.create_task(_snapshot_loop())
    asyncio.create_task(_notification_loop())
    asyncio.create_task(_rating_refresh_loop())
    yield
    # Shutdown
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


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s %s — %s", request.method, request.url.path, exc, exc_info=exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


app.include_router(auth_router)
app.include_router(bonds_router)
app.include_router(pdf_router)
app.include_router(portfolios_router)
app.include_router(portfolio_router)
app.include_router(settings_router)
app.include_router(admin_router)
app.include_router(tbank_router)
app.include_router(watchlist_router)


@app.get("/manifest.json")
async def manifest():
    path = _ui_dir / "manifest.json"
    return Response(
        path.read_text(encoding="utf-8"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/robots.txt")
async def robots_txt():
    content = (
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
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/privacy</loc>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/terms</loc>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.3</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(content, media_type="application/xml")


@app.get("/og-image.png")
async def og_image():
    path = _ui_dir / "og-image.png"
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/icon-192.png")
async def icon_192():
    path = _ui_dir / "icon-192.png"
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/icon-512.png")
async def icon_512():
    path = _ui_dir / "icon-512.png"
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon-32.png")
async def favicon_32():
    path = _ui_dir / "favicon-32.png"
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    path = _ui_dir / "apple-touch-icon.png"
    return Response(
        path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js")
async def service_worker():
    path = _ui_dir / "sw.js"
    return Response(
        path.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(
        landing_path.read_text(encoding="utf-8"),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    return HTMLResponse(
        privacy_path.read_text(encoding="utf-8"),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/terms", response_class=HTMLResponse)
async def terms() -> HTMLResponse:
    return HTMLResponse(
        terms_path.read_text(encoding="utf-8"),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/app", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(
        dashboard_path.read_text(encoding="utf-8"),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/landing", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """Kept for backwards compatibility — redirects to /."""
    return HTMLResponse(
        landing_path.read_text(encoding="utf-8"),
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

    if portfolio.get("share_expires_at") and portfolio["share_expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Ссылка устарела")

    if portfolio["share_password_hash"]:
        if not x_share_password:
            raise HTTPException(status_code=403, detail="Требуется пароль")
        if not bcrypt.checkpw(x_share_password.encode(), portfolio["share_password_hash"].encode()):
            raise HTTPException(status_code=403, detail="Неверный пароль")

    rows = await portfolio_service.get_table(portfolio["id"])
    return {"items": rows}


@app.get("/share/{share_token}/snapshots")
async def get_shared_portfolio_snapshots(
    share_token: str,
    days: int = 90,
    x_share_password: str | None = Header(None),
) -> list[dict]:
    """Portfolio value history for shared view (public, no auth required)."""
    portfolio = storage_service.get_portfolio_by_share_token(share_token)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Портфель не найден")

    if portfolio.get("share_expires_at") and portfolio["share_expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Ссылка устарела")

    if portfolio["share_password_hash"]:
        if not x_share_password:
            raise HTTPException(status_code=403, detail="Требуется пароль")
        if not bcrypt.checkpw(x_share_password.encode(), portfolio["share_password_hash"].encode()):
            raise HTTPException(status_code=403, detail="Неверный пароль")

    if days not in (30, 90, 365):
        days = 90
    return storage_service.get_portfolio_snapshots(portfolio["id"], days)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
