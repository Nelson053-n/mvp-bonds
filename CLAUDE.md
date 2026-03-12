# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run application:**
```bash
MVP_JWT_SECRET=dev_secret_key_12345 .venv/bin/uvicorn app.main:app --reload
```

**Run tests:**
```bash
.venv/bin/python -m pytest                        # All tests with coverage
.venv/bin/python -m pytest tests/test_api.py      # Single file
.venv/bin/python -m pytest tests/test_api.py::test_health  # Single test
.venv/bin/python -m pytest -v -s                  # Verbose with output
```

**Python environment:** `.venv/bin/python3`

**Database inspection:**
```bash
sqlite3 data/portfolio.db ".tables"
sqlite3 data/portfolio.db "SELECT * FROM users;"
```

## Architecture

**Stack:** FastAPI (async) + SQLite + vanilla HTML/CSS/JS (single-page app in one file)

**Request flow:**
1. `app/main.py` — registers routers, starts/stops background tasks via lifespan
2. `app/api/` — thin API layer, delegates to services; auth via `app/api/deps.py` (FastAPI `Depends()`)
3. `app/services/portfolio_service.py` — central orchestrator that coordinates all other services
4. `app/services/moex_service.py` — fetches live market data from MOEX ISS API + credit ratings (SmartLab → MOEX fallback)
5. `app/services/storage_service.py` — SQLite persistence (auto-creates schema + admin user on first run)
6. `app/services/cache_service.py` — in-memory cache with background refresh (configurable interval, default 900s)
7. `app/services/llm_service.py` — generates AI comments; two modes: `stub` (hardcoded) or `openai`
8. `app/services/notification_service.py` — Telegram alerts (coupon reminders, double-downgrade detection)
9. `app/ui/dashboard.html` — entire frontend SPA in a single file (HTML + CSS + JS)

**Background tasks** (started via lifespan in `app/main.py`):
- DB backup on startup (rolling, keeps 3 most recent in `data/backups/`)
- Expired share token cleanup (hourly)
- Daily portfolio snapshots (01:05 UTC)
- Coupon notification check (every 6 hours)
- Credit rating refresh (daily 03:00 UTC, with Telegram alerts on double downgrade)
- Cache refresh (periodic MOEX data refresh)

**Database:** SQLite at `data/portfolio.db` (configurable via `MVP_SQLITE_DB_PATH`). Schema auto-created; migrations run on startup. PRAGMA: `journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`. Tables: `users`, `portfolios`, `portfolio_items`, `price_snapshots`, `coupon_notifications`, `price_alerts`, `portfolio_snapshots`, `app_settings`, `rate_limits`, `watchlist`, `rating_history`.

**Auth:** JWT tokens (PyJWT, HS256, 72h expiry) + bcrypt passwords. First startup bootstraps an admin user with an auto-generated password printed to logs.

**Dependency injection** (`app/api/deps.py`):
- `get_current_user()` — extract/verify JWT from Bearer token
- `get_admin_user()` — require authenticated admin
- `get_portfolio_or_403()` — check portfolio ownership

## Key patterns

**Services are singletons:** Imported directly from their modules (e.g., `from app.services.storage_service import storage_service`).

**MOEX bond endpoint:** Universal endpoint for all bond types (TQCB + TQOB):
```
/engines/stock/markets/bonds/securities/{secid}.json
```

**Price fallback chain:** `LAST → LCLOSE → PREVPRICE → PREVWAPRICE → PREVLEGALCLOSEPRICE`

**Coupon date logic:** Uses `NEXTCOUPON` anchor from MOEX; holiday-aware; `seenMonths` Set prevents duplicate payments in the same calendar month.

**FX conversion:** Non-RUB bonds (USD, EUR, CNY, etc.) auto-converted to RUB via MOEX futures indicative rates API. FX rates cached in-memory with 1h TTL; stale cache used as fallback. `BondSnapshot.face_unit` carries original currency, `fx_rate` the conversion rate.

**Soft-delete:** `portfolio_items.deleted_at` (ISO 8601 timestamp). All queries filter `WHERE deleted_at IS NULL`. Recovery via `POST /portfolios/{id}/instruments/{item_id}/restore`.

**Audit logging:** All data mutations logged at INFO level with `AUDIT` prefix: `AUDIT add_item`, `AUDIT delete_item`, `AUDIT update_item`, `AUDIT restore_item`, etc. Includes affected IDs, field changes, and result counts.

**Schema migrations:** Inline in `storage_service._ensure_db()` — no migration files. Pattern: `ALTER TABLE ... ADD COLUMN` wrapped in `try/except OperationalError: pass`. New tables created with `IF NOT EXISTS`.

**LLM mode:** Controlled by `MVP_LLM_MODE` env var (`stub` or `openai`). Tests always use `stub`.

**Portfolio sharing:** Via tokens stored in SQLite; optional password protection + expiry.

**Rating sources (priority):** SmartLab → MOEX → LISTLEVEL proxy → None. Double-downgrade detection triggers Telegram alert.

**Error hierarchy** (`app/exceptions.py`): `AppError` base → `ValidationError`, `NotFoundError`, `MOEXError` (→ `PriceNotFoundError`, `DataFetchError`), `SmartLabError` (→ `RatingNotFoundError`), `PortfolioError` (→ `InstrumentNotFoundError`), `CacheError`, `AuthError`. Mapped to HTTP responses with Russian error messages.

## API routers

| Router | Prefix | Key endpoints |
|---|---|---|
| `auth.py` | `/auth` | register, login, me |
| `portfolios.py` | `/portfolios` | CRUD, share/unshare, table, snapshots, validate |
| `portfolio.py` | `/portfolios/{id}/instruments` | add/update/delete instruments, manual coupon |
| `bonds.py` | `/bonds` | search with ratings |
| `pdf.py` | `/pdf` | export portfolio to PDF |
| `settings.py` | `/settings` | user settings (Telegram, price alerts) |
| `admin.py` | `/admin` | stats, data sources, user management |
| `watchlist.py` | `/watchlist` | watchlist CRUD (track bonds/stocks without adding to portfolio) |

Public (no auth): `GET /share/{token}`, `GET /share/{token}/table`, `GET /share/{token}/snapshots`

## Configuration (env vars, prefix `MVP_`)

| Variable | Required | Default |
|---|---|---|
| `MVP_JWT_SECRET` | Yes | — |
| `MVP_SQLITE_DB_PATH` | No | `data/portfolio.db` |
| `MVP_LLM_MODE` | No | `stub` |
| `MVP_OPENAI_API_KEY` | If llm_mode=openai | — |
| `MVP_OPENAI_BASE_URL` | No | `https://api.openai.com/v1` |
| `MVP_OPENAI_MODEL` | No | `gpt-4o-mini` |
| `MVP_LOG_LEVEL` | No | `INFO` |
| `MVP_LOG_FORMAT` | No | `json` |
| `MVP_MOEX_BASE_URL` | No | `https://iss.moex.com/iss` |
| `MVP_TG_BOT_TOKEN` | No | — |
| `MVP_TG_CHAT_ID` | No | — |

SMTP variables (`MVP_SMTP_*`) are optional for password reset emails.

## Design system (UI)

The frontend (`app/ui/dashboard.html`) uses:
- Font: Inter (Google Fonts)
- CSS variables: `--slate-50…--slate-900`, `--blue-500/600/700`, `--green-600`, `--red-600`
- Radii: `--radius-sm` 6px / `--radius` 8px / `--radius-lg` 12px
- Transitions: `.15s` interactive, `.1s` transform
- Design inspired by HeroUI/shadcn principles — but implemented in pure vanilla CSS/JS (no React)

## Tests

Tests use dependency injection via `app/api/deps.py` overrides. `tests/conftest.py` provides:
- Temporary SQLite DB per test session
- `settings_override` fixture forces `stub` LLM mode
- `client` fixture — `httpx.AsyncClient` with ASGI transport
- `test_auth_token` / `auth_headers` — valid JWT + headers for admin user
- `sample_stock_input` / `sample_bond_input` — test data fixtures

**pytest config** (`pyproject.toml`): `asyncio_mode = "auto"`, coverage on `app/` with HTML report in `htmlcov/`
