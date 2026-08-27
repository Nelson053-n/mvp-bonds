# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run application:**
```bash
MVP_JWT_SECRET=<YOUR_JWT_SECRET> .venv/bin/uvicorn app.main:app --reload
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

**Stack:** FastAPI (async) + SQLite + vanilla HTML/CSS/JS (SPA: `dashboard.html` shell + static assets)

**Request flow:**
1. `app/main.py` — registers routers, starts/stops background tasks via lifespan
2. `app/api/` — thin API layer, delegates to services; auth via `app/api/deps.py` (FastAPI `Depends()`)
3. `app/services/portfolio_service.py` — central orchestrator that coordinates all other services
4. `app/services/moex_service.py` — fetches live market data from MOEX ISS API + credit ratings (SmartLab → MOEX fallback)
5. `app/services/storage_service.py` — SQLite persistence (auto-creates schema + admin user on first run)
6. `app/services/cache_service.py` — in-memory cache with background refresh (configurable interval, default 900s)
7. `app/services/llm_service.py` — generates AI comments; two modes: `stub` (hardcoded) or `openai`
8. `app/services/notification_service.py` — Telegram alerts (coupon reminders, double-downgrade detection)
9. `app/ui/dashboard.html` — HTML shell of the frontend SPA (~1450 lines); JS/CSS live in `app/ui/static/` (`app.js`, `translations.js`, `styles.css`). Service worker `app/ui/sw.js` caches static assets — after ANY change to static files bump `CACHE_NAME` in `sw.js`, otherwise clients keep the old version

**Background tasks** (started via lifespan in `app/main.py`):
- DB backup on startup (rolling, keeps 3 most recent in `data/backups/`)
- Expired share token cleanup (hourly)
- Daily portfolio snapshots (01:05 UTC)
- Coupon notification check (every 6 hours)
- Credit rating refresh (daily 03:00 UTC, with Telegram alerts on double downgrade)
- Cache refresh (periodic MOEX data refresh)

**Database:** SQLite at `data/portfolio.db` (configurable via `MVP_SQLITE_DB_PATH`). Schema auto-created; migrations run on startup. PRAGMA: `journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`. Tables: `users`, `portfolios`, `portfolio_items`, `price_snapshots`, `coupon_notifications`, `price_alerts`, `portfolio_snapshots`, `app_settings`, `rate_limits`, `watchlist`, `rating_history`.

**Auth:** JWT tokens (PyJWT, HS256, 72h expiry) + bcrypt passwords. First startup bootstraps an admin user with an auto-generated password printed to logs.

**Token encryption** (`app/services/crypto_utils.py`): T-Bank API tokens are stored in `portfolio_sync.tbank_token_enc` encrypted with Fernet. The key is derived from `MVP_TOKEN_ENC_KEY` — kept SEPARATE from `MVP_JWT_SECRET` so rotating the JWT secret does NOT break stored tokens. If `MVP_TOKEN_ENC_KEY` is unset it falls back to a `jwt_secret`-derived key (legacy). Decryption tries the active key first, then the legacy key; on a successful legacy read the token is re-encrypted under the active key (lazy migration via `decrypt_and_maybe_migrate` + `storage_service.update_sync_token`).

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

**Schema migrations:** Versioned, no migration files. `storage_service.SCHEMA_VERSION` + `_run_migrations()` holding a `(N, self._migration_vN)` list; each step is idempotent (`IF NOT EXISTS` / `try/except OperationalError: pass`) and gets its own `conn.commit()`.

To add a column: bump `SCHEMA_VERSION`, append the pair, write `_migration_vN(conn)`. Do NOT add `ALTER TABLE` to the older block inside `_ensure_db()` — nothing commits after it, so the DDL is rolled back when the connection closes and the column silently never appears.

Note the deploy interaction: migrations run in the lifespan startup, and the default graceful-reload (SIGHUP) does not re-run it. A new column needs `systemctl restart bondai` (or `DEPLOY_FORCE_RESTART=1 ops/deploy.sh`).

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
| `MVP_TOKEN_ENC_KEY` | No | — (falls back to `MVP_JWT_SECRET`) |
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

`MVP_TG_BOT_TOKEN` powers the public bond-search Telegram bot (`app/services/telegram_bot_service.py`): when set, a long-polling loop starts in the leader worker and answers `/start`, `/help` and free-text ticker/name queries with a bond card. Unset → bot silently disabled. SMTP variables (`MVP_SMTP_*`) are optional for password reset emails.

## Прод и публикация

**Два прода:**
- Локальный: `root@192.168.10.114`, каталог `/opt/mvp-bonds` (git remote `prod`).
- Публичный: **bondai.ru** = `root@212.8.228.248`, каталог `/opt/mvp-bonds`, systemd-сервис `bondai`.

**Автодеплой на bondai.ru:** systemd timer `bondai-autodeploy` (раз в 5 мин) — git fetch, сравнение с `origin/v3`, при новых коммитах `ops/deploy.sh` (smoke-проверка + откат) и TG-уведомление. Лог: `/var/log/bondai-autodeploy.log`. Деплой = обычный `git push` в `origin` (ветка `v3`), руками на сервер ходить не нужно.

**Публикация отчётов без логина:** HTML-отчёт кладётся на прод в `app/ui/static/` как `report-<случайный-токен>.html` → доступен по `https://bondai.ru/static/report-<token>.html`. Файлы НЕ в git, поэтому автодеплой их не затирает. Удаление: `ssh root@212.8.228.248 rm /opt/mvp-bonds/app/ui/static/report-*.html`.

**Доставка отчётов — Telegram, НЕ почта** (решение пользователя, 2026-07-10): ссылку на отчёт слать через TG-бота проекта **@bondinfoai_bot**; токен и chat_id — в SQLite `app_settings` (`tg_bot_token`/`tg_chat_id`, задаются в UI панели), НЕ в env. Готовая команда — skill `portfolio-report`. На проде нет `sqlite3` CLI — БД читать через `.venv/bin/python3`. Почту не поднимать: Gmail MCP умеет только черновики, SMTP с сервера на порт 25 отбивается (550, нет PTR), `MVP_SMTP_*` сознательно не настраиваем.

## Design system (UI)

The frontend (`app/ui/dashboard.html` + `app/ui/static/styles.css`) uses:
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
