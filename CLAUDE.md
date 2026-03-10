# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run application:**
```bash
MVP_JWT_SECRET=dev_secret_key_12345_abcdefghijklmn .venv/bin/uvicorn app.main:app --reload
```

**Run tests:**
```bash
.venv/bin/python -m pytest                        # All tests with coverage
.venv/bin/python -m pytest tests/test_api.py      # Single file
.venv/bin/python -m pytest tests/test_api.py::TestPortfolioAPI::test_health_endpoint  # Single test
.venv/bin/python -m pytest -v                     # Verbose
```

**Python environment:** `.venv/bin/python3`

## Architecture

**Stack:** FastAPI (async) + SQLite + vanilla HTML/CSS/JS (single-page app in one file)

**Request flow:**
1. `app/main.py` — registers routers, starts/stops background cache refresh via lifespan
2. `app/api/` — thin API layer, delegates to services
3. `app/services/portfolio_service.py` — central orchestrator that coordinates all other services
4. `app/services/moex_service.py` — fetches live market data from MOEX ISS API
5. `app/services/storage_service.py` — SQLite persistence (auto-creates schema + admin user on first run)
6. `app/services/cache_service.py` — background thread that periodically refreshes portfolio metrics in memory
7. `app/services/llm_service.py` — generates AI comments; two modes: `stub` (hardcoded) or `openai`
8. `app/ui/dashboard.html` — entire frontend in a single file (HTML + CSS + JS)

**Database:** SQLite at `data/portfolio.db` (configurable via `MVP_SQLITE_DB_PATH`). Schema is created automatically; migrations run on startup.

**Auth:** JWT tokens (PyJWT) + bcrypt passwords. First startup bootstraps an admin user with an auto-generated password printed to logs.

## Key patterns

**MOEX bond endpoint:** Universal endpoint for all bond types (TQCB + TQOB):
```
/engines/stock/markets/bonds/securities/{secid}.json
```

**Price fallback chain:** `LAST → LCLOSE → PREVPRICE → PREVWAPRICE → PREVLEGALCLOSEPRICE`

**Coupon date logic:** Uses `NEXTCOUPON` anchor from MOEX; holiday-aware; `seenMonths` Set prevents duplicate payments in the same calendar month.

**LLM mode:** Controlled by `MVP_LLM_MODE` env var (`stub` or `openai`). Tests always use `stub`.

**Portfolio sharing:** Via one-time tokens stored in SQLite; optional password protection.

## Configuration (env vars, prefix `MVP_`)

| Variable | Required | Default |
|---|---|---|
| `MVP_JWT_SECRET` | Yes | — |
| `MVP_SQLITE_DB_PATH` | No | `data/portfolio.db` |
| `MVP_LLM_MODE` | No | `stub` |
| `MVP_OPENAI_API_KEY` | If llm_mode=openai | — |
| `MVP_LOG_LEVEL` | No | `INFO` |
| `MVP_LOG_FORMAT` | No | `text` |

SMTP variables (`MVP_SMTP_*`) are optional for email notifications.

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
- `client` fixture — `httpx.AsyncClient` for testing FastAPI endpoints
- `test_auth_token` fixture — valid JWT for the admin user
