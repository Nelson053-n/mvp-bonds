from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.api.auth import router as auth_router
from app.api.bond_pages import router as bond_pages_router
from app.api.bonds import router as bonds_router
from app.api.calculators import router as calculators_router
from app.api.pdf import router as pdf_router
from app.api.portfolio import router as portfolio_router
from app.api.portfolios import router as portfolios_router
from app.api.settings import router as settings_router
from app.api.admin import router as admin_router
from app.api.tbank import router as tbank_router
from app.api.waitlist import router as waitlist_router
from app.api.watchlist import router as watchlist_router
from app.api.deps import get_shared_portfolio
from app.config import settings
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
        # Yandex.Metrika audience-sync pixels load from yandex.ru/an
        "img-src 'self' data: https://mc.yandex.ru https://mc.yandex.com https://yandex.ru; "
        # Webvisor uses a websocket (wss) to mc.yandex.ru
        "connect-src 'self' https://mc.yandex.ru https://mc.yandex.com wss://mc.yandex.ru; "
        # Metrika opens an iframe on mc.yandex.ru for session replay
        "frame-src https://mc.yandex.ru; "
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

        application.api_route(url_path, methods=["GET", "HEAD"])(_make_handler())


# ── Background tasks ────────────────────────────────────────────────────────

async def _cleanup_shares_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            storage_service.cleanup_expired_shares()
        except Exception:
            pass


async def _indexnow_loop():
    """Submit all public URLs to IndexNow (Yandex/Bing) once a day.

    Waits a short delay after startup so the bonds cache is warm, then resubmits
    daily. Best-effort: never raises.
    """
    from app.services import indexnow_service
    from app.api.bond_pages import all_public_urls
    await asyncio.sleep(120)  # let the per-process bonds cache warm first
    while True:
        try:
            urls = await all_public_urls()
            if urls:
                await indexnow_service.submit(urls)
        except Exception:
            logger.warning("IndexNow loop iteration failed", exc_info=True)
        await asyncio.sleep(24 * 3600)


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


async def _benchmark_snapshot_loop():
    """Persist RGBI snapshot daily at 23:00 UTC. On startup, backfill last year."""
    from datetime import datetime, timezone, date
    from app.services.moex_service import moex_service

    # On startup: backfill if we have fewer than 100 RGBI points
    try:
        existing = storage_service.get_benchmark_snapshots("RGBI", days=400)
        if len(existing) < 100:
            history = await moex_service.get_index_history("RGBI", days=400)
            if history:
                storage_service.bulk_upsert_benchmark_snapshots("RGBI", history)
                logger.info("RGBI backfill: %d points", len(history))
    except Exception:
        logger.exception("RGBI backfill failed")

    while True:
        now = datetime.now(timezone.utc)
        secs_to_23 = ((23 - now.hour) % 24) * 3600 - now.minute * 60 - now.second
        if secs_to_23 <= 0:
            secs_to_23 += 86400
        await asyncio.sleep(secs_to_23)
        try:
            value = await moex_service.get_index_value("RGBI")
            if value is not None:
                storage_service.upsert_benchmark_snapshot("RGBI", date.today().isoformat(), value)
                logger.info("RGBI snapshot saved: %.2f", value)
        except Exception:
            logger.exception("RGBI snapshot failed")


async def _daily_backup_loop():
    """Create a daily automatic backup at the configured hour (UTC)."""
    from datetime import datetime, timezone
    while True:
        try:
            hour = int(storage_service.get_setting("backup_daily_hour", "2"))
        except ValueError:
            hour = 2
        now = datetime.now(timezone.utc)
        secs_to_hour = ((hour - now.hour) % 24) * 3600 - now.minute * 60 - now.second
        if secs_to_hour <= 0:
            secs_to_hour += 86400
        await asyncio.sleep(secs_to_hour)
        try:
            storage_service.create_backup(label="auto")
            logger.info("Daily auto backup completed")
        except Exception:
            logger.exception("Daily auto backup failed")


def _backup_db_on_startup() -> None:
    """Create a rolling backup of the SQLite database on startup. Keeps 3 most recent.

    Startup backups are tagged with a `_startup` suffix and rotated independently
    from the daily `_auto` / manual backups, so frequent restarts can never evict
    the longer-lived daily history.
    """
    import shutil
    from datetime import datetime, timezone

    db_path = Path(storage_service.db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"portfolio_{stamp}_startup.db"
    try:
        shutil.copy2(db_path, dst)
        logger.info("DB backup created: %s", dst)
        # Rotate ONLY startup backups (keep 3); never touch _auto/_manual ones.
        backups = sorted(backup_dir.glob("portfolio_*_startup.db"))
        for old in backups[:-3]:
            old.unlink()
            logger.info("Old startup backup removed: %s", old)
    except Exception:
        logger.exception("Failed to create DB backup")


# ── Application ─────────────────────────────────────────────────────────────

def _proc_cmdline(pid: int) -> str | None:
    """Return /proc/<pid>/cmdline as a string, or None if unavailable (non-Linux,
    no permission, or process gone)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


# Marker identifying our own server process — used to detect PID reuse: a live PID
# whose cmdline doesn't look like ours is a recycled PID, not the old leader.
_LEADER_MARKER = "uvicorn"


def _acquire_leader_lock() -> bool:
    """Try to become the single 'leader' worker for DB-writing background tasks.

    With uvicorn --workers N, the lifespan runs in every worker. Tasks that write
    to the shared SQLite DB (snapshots, notifications, backups, …) must run in
    exactly one worker. We elect a leader via an atomic O_CREAT|O_EXCL lock file;
    a stale file from a crashed leader is reclaimed when its PID is dead OR the
    PID is alive but has been recycled by an unrelated process (verified via
    /proc/<pid>/cmdline, so a reused PID can't permanently block leadership).
    """
    lock_path = Path(storage_service.db_path).parent / ".leader.lock"

    def _try_create() -> bool:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, f"{os.getpid()}:{_LEADER_MARKER}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    if _try_create():
        return True

    # Lock exists — decide whether the holder is genuinely still alive.
    def _holder_alive() -> bool:
        try:
            raw = lock_path.read_text().strip()
        except FileNotFoundError:
            return False  # vanished — treat as free
        pid_str = raw.split(":", 1)[0]
        try:
            holder = int(pid_str)
        except ValueError:
            return False  # malformed lock → reclaimable
        try:
            os.kill(holder, 0)  # raises if PID not alive
        except (ProcessLookupError, ValueError):
            return False  # dead PID
        except PermissionError:
            # PID alive but owned by another user → almost certainly recycled,
            # not our worker. Confirm via cmdline when possible.
            cmd = _proc_cmdline(holder)
            return cmd is not None and _LEADER_MARKER in cmd
        # PID alive and signalable. Guard against PID reuse: if we can read the
        # cmdline and it isn't one of ours, the PID was recycled → reclaim.
        cmd = _proc_cmdline(holder)
        if cmd is not None and _LEADER_MARKER not in cmd and "python" not in cmd:
            return False
        return True

    if _holder_alive():
        return False  # genuine leader present → we are a follower
    # Stale or recycled — reclaim.
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return _try_create()


def _release_leader_lock() -> None:
    """Remove the lock on graceful shutdown — but only if it still holds OUR pid,
    so we never delete a lock another worker may have legitimately taken over."""
    lock_path = Path(storage_service.db_path).parent / ".leader.lock"
    try:
        raw = lock_path.read_text().strip()
    except FileNotFoundError:
        return
    if raw.split(":", 1)[0] == str(os.getpid()):
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up application")
    # Per-process cache warms in EVERY worker (it serves that worker's requests).
    cache_service.start_background()

    is_leader = _acquire_leader_lock()
    if is_leader:
        logger.info("This worker is the background-task leader (pid=%s)", os.getpid())
        _backup_db_on_startup()
        asyncio.create_task(_cleanup_shares_loop())
        asyncio.create_task(_snapshot_loop())
        asyncio.create_task(_notification_loop())
        asyncio.create_task(_rating_refresh_loop())
        asyncio.create_task(_tbank_sync_loop())
        asyncio.create_task(_daily_backup_loop())
        asyncio.create_task(_benchmark_snapshot_loop())
        # Public Telegram bond-search bot — only in the leader so a single
        # getUpdates poller exists per token (avoids 409 conflicts). No-op if
        # MVP_TG_BOT_TOKEN is unset.
        from app.services.telegram_bot_service import telegram_bot_service
        if telegram_bot_service.enabled:
            logger.info("Starting Telegram bond-search bot")
            asyncio.create_task(telegram_bot_service.run_polling())
        # IndexNow: daily-resubmit all public URLs to Yandex/Bing so new bond
        # pages index within minutes. Leader-only (one submit per deploy). No-op
        # if MVP_INDEXNOW_KEY is unset.
        from app.services import indexnow_service
        if indexnow_service.enabled():
            logger.info("IndexNow enabled — scheduling daily URL submission")
            asyncio.create_task(_indexnow_loop())
    else:
        logger.info("This worker is a follower — DB-writing background tasks skipped")
    yield
    logger.info("Shutting down application")
    cache_service.stop_background()
    if is_leader:
        try:
            from app.services.telegram_bot_service import telegram_bot_service
            telegram_bot_service.stop()
        except Exception:
            pass
        _release_leader_lock()
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
app.include_router(bond_pages_router)
app.include_router(bonds_router)
app.include_router(calculators_router)
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
        "Sitemap: https://bondai.ru/sitemap-bonds.xml\n"
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
        "  <url>\n"
        "    <loc>https://bondai.ru/uchebnik</loc>\n"
        "    <lastmod>2026-06-14</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.7</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/bond</loc>\n"
        "    <lastmod>2026-06-14</lastmod>\n"
        "    <changefreq>daily</changefreq>\n"
        "    <priority>0.8</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/calc</loc>\n"
        "    <lastmod>2026-06-14</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.6</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/calc/nkd</loc>\n"
        "    <lastmod>2026-06-14</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.7</priority>\n"
        "  </url>\n"
        "  <url>\n"
        "    <loc>https://bondai.ru/calc/ytm</loc>\n"
        "    <lastmod>2026-06-14</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.7</priority>\n"
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
        "- [Учебник по облигациям](https://bondai.ru/uchebnik): Что такое YTM (доходность к погашению), НКД, оферта, виды облигаций (ОФЗ, корпоративные, ВДО), флоатеры и риски — образовательный материал для инвесторов\n"
        "- [Каталог облигаций MOEX](https://bondai.ru/bond): Все торгуемые облигации Московской биржи (ОФЗ и корпоративные) — у каждой бумаги своя страница /bond/{тикер} с ценой, доходностью YTM, купонами, офертой и кредитным рейтингом\n"
        "- [Калькулятор НКД](https://bondai.ru/calc/nkd): Онлайн-расчёт накопленного купонного дохода облигации по ставке или размеру купона\n"
        "- [Калькулятор доходности YTM](https://bondai.ru/calc/ytm): Онлайн-расчёт эффективной, простой и текущей доходности облигации к погашению\n"
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


# ── Search-engine verification & IndexNow ───────────────────────────────────
# Yandex.Webmaster file-method: it expects /yandex_<token>.html to return a body
# containing the token. We serve exactly the file Yandex looks for, sourced from
# MVP_YANDEX_VERIFICATION, so verification needs only an .env value (no redeploy).
@app.api_route("/yandex_{token}.html", methods=["GET", "HEAD"])
async def yandex_verification(token: str):
    expected = settings.yandex_verification
    if not expected or token != expected:
        return Response("Not Found", status_code=404, media_type="text/plain")
    # Serve byte-for-byte the file Yandex.Webmaster generated (it checks the
    # body contains "Verification: <token>").
    body = (
        "<html>\n"
        "    <head>\n"
        '        <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n'
        "    </head>\n"
        "    <body>Verification: " + expected + "</body>\n"
        "</html>\n"
    )
    return Response(body, media_type="text/html")


# Google Search Console file-method: GSC checks /google<token>.html contains the
# line "google-site-verification: google<token>.html". Served from
# MVP_GOOGLE_VERIFICATION (the bare token, without the "google" prefix/suffix).
@app.api_route("/google{token}.html", methods=["GET", "HEAD"])
async def google_verification(token: str):
    expected = settings.google_verification
    if not expected or token != expected:
        return Response("Not Found", status_code=404, media_type="text/plain")
    return Response("google-site-verification: google" + expected + ".html",
                    media_type="text/html")


# IndexNow: serving /<key>.txt (body = the key) proves key ownership to
# Yandex/Bing, which then accept instant index-submission pings for our URLs.
@app.api_route("/{key}.txt", methods=["GET", "HEAD"])
async def indexnow_key_file(key: str):
    configured = settings.indexnow_key
    if configured and key == configured:
        return Response(configured, media_type="text/plain")
    return Response("Not Found", status_code=404, media_type="text/plain")


# ── HTML pages ──────────────────────────────────────────────────────────────

# Public, static marketing/legal pages — cacheable at the edge (improves GEO/crawl).
_PUBLIC_HTML_PAGES: dict[str, Path] = {
    "/":        landing_path,
    "/landing": landing_path,
    "/privacy": _ui_dir / "privacy.html",
    "/terms":   _ui_dir / "terms.html",
    "/uchebnik": _ui_dir / "uchebnik.html",
}
# Private app shells — never cache (per-user data is fetched client-side).
_PRIVATE_HTML_PAGES: dict[str, Path] = {
    "/app":          dashboard_path,
    "/all":          dashboard_path,
    "/admin/promo":  dashboard_path,  # deep-link to the promo-materials section
}
# 5min freshness + day-long stale-while-revalidate: deploys propagate in minutes,
# repeat views render instantly from cache while the browser revalidates in the
# background. (Plain max-age=3600 made redesigns invisible for up to an hour.)
_PUBLIC_CACHE_HEADERS = {"Cache-Control": "public, max-age=300, stale-while-revalidate=86400"}

for _page_url, _page_path in {**_PUBLIC_HTML_PAGES, **_PRIVATE_HTML_PAGES}.items():
    _headers = _PUBLIC_CACHE_HEADERS if _page_url in _PUBLIC_HTML_PAGES else _NO_CACHE_HEADERS
    def _make_page_handler(fp: Path = _page_path, hdrs: dict = _headers):
        async def handler() -> HTMLResponse:
            return HTMLResponse(fp.read_text(encoding="utf-8"), headers=hdrs)
        return handler
    app.api_route(_page_url, response_class=HTMLResponse, methods=["GET", "HEAD"])(_make_page_handler())


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
