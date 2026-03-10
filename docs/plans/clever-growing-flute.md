# Plan: Senior Full-Stack Audit & Improvement Sprint

## Context

Комплексное улучшение проекта Bond AI (mvp-bonds) после внедрения v3pay ветки.
Задача — привести проект к production-ready состоянию по четырём направлениям:
безопасность, i18n, тесты, UX.

Приоритеты (по ответам пользователя):
1. Безопасность — критические уязвимости
2. i18n — все тексты через систему переводов (RU/EN)
3. Тесты — покрытие v3pay + security
4. UX — ARIA, toast вместо alert(), retry кнопки

---

## Critical Files

| Файл | Изменения |
|------|-----------|
| `app/config.py` | min_length=32 для jwt_secret |
| `app/api/auth.py` | password min_length=8, email pattern |
| `app/services/auth_service.py` | reset codes → SQLite |
| `app/services/storage_service.py` | таблица reset codes, 4 индекса, методы |
| `app/main.py` | CSP+HSTS headers, global 500 handler, rate limit share |
| `app/ui/dashboard.html` | i18n Pro-modal + admin strings, ARIA, toast |
| `tests/test_api.py` | 8 новых тест-кейсов |

---

## Phase 1 — Безопасность

### 1.1 JWT Secret — минимальная длина 32 символа
`app/config.py:13`
```python
jwt_secret: str = Field(..., min_length=32)
```

### 1.2 Пароль — мягкая политика (8+ символов)
`app/api/auth.py:20` — изменить с 6 на 8:
```python
password: str = Field(..., min_length=8)
```

### 1.3 Email валидация
`app/api/auth.py` в `ChangeEmailInput`:
```python
email: str = Field(..., max_length=254, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
```

### 1.4 Security Headers — CSP + HSTS
`app/main.py:41-47` — добавить в `_SECURITY_HEADERS`:
```python
"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
"Content-Security-Policy": (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
),
```

### 1.5 Global 500 Exception Handler
`app/main.py` — после `app = FastAPI(...)`:
```python
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled: %s %s", request.method, request.url, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
```

### 1.6 Rate Limiting на публичных share endpoints
`app/main.py` — в `/share/{share_token}/table` и `/share/{share_token}/snapshots`:
```python
rate_key = f"share:{share_token}:{request.client.host if request.client else 'unknown'}"
if not storage_service.check_rate_limit(rate_key, 60, 30):  # 30 req/min per token per IP
    raise HTTPException(status_code=429, detail="Too many requests")
```

### 1.7 Reset Codes → SQLite
`app/services/storage_service.py` — добавить в `_migrate()`:
```sql
CREATE TABLE IF NOT EXISTS password_reset_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_reset_codes_expires ON password_reset_codes(expires_at);
```

Новые методы в `StorageService`:
```python
def save_reset_code(self, code: str, user_id: int, expires_at: int) -> None
def get_reset_code(self, code: str) -> dict | None   # {user_id, expires_at}
def delete_reset_code(self, code: str) -> None
def cleanup_reset_codes(self) -> None  # DELETE WHERE expires_at < now()
```

`app/services/auth_service.py` — убрать `_reset_codes: dict`, `RESET_TTL`, `_cleanup_reset_codes()`.
Переписать `request_password_reset()` и `confirm_password_reset()` через storage_service методы.

### 1.8 Admin Password — не в логи
`app/services/storage_service.py:~290`:
```python
import sys
print(f"\n{'='*60}\nAdmin credentials (first startup only):\n  Username: admin\n  Password: {admin_password}\n{'='*60}\n",
      file=sys.stderr, flush=True)
logger.info("Admin user created — credentials printed to stderr")
```

### 1.9 DB Indices
`app/services/storage_service.py` в `_migrate()`:
```sql
CREATE INDEX IF NOT EXISTS idx_coupon_notif_item ON coupon_notifications(item_id);
CREATE INDEX IF NOT EXISTS idx_alerts_item_id ON price_alerts(item_id);
CREATE INDEX IF NOT EXISTS idx_alerts_user_triggered ON price_alerts(user_id, triggered);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
```

---

## Phase 2 — i18n

### 2.1 Новые ключи переводов

Добавить в обе секции (ru/en) в `dashboard.html`:

**Pro/Upgrade Modal:**
```
upgrade.title       / upgrade modal header
upgrade.subtitle    / subtitle
upgrade.freePlan    / "📦 Бесплатный" / "📦 Free"
upgrade.proPlan     / "⭐ Pro"
upgrade.feat.portfoliosFree   "1 портфель" / "1 portfolio"
upgrade.feat.instrumentsFree  "10 бумаг" / "10 instruments"
upgrade.feat.aiNo / pdfNo / notifNo / shareNo
upgrade.feat.portfoliosUnlim  "∞ Портфелей" / "∞ Portfolios"
upgrade.feat.instrumentsUnlim "∞ Бумаг" / "∞ Instruments"
upgrade.feat.aiYes / pdfYes / notifYes / shareYes
upgrade.pricingMonth "299 ₽/мес" / "299 ₽/mo"
upgrade.pricingYear  "2 490 ₽/год" / "2 490 ₽/yr"
upgrade.discount     "−30%"
upgrade.cta          "Подключить Pro →" / "Subscribe Pro →"
upgrade.stubMsg      "Оплата временно недоступна..." / "Payment temporarily unavailable..."
```

**Plan Card:**
```
plan.free / plan.pro
plan.card.freeDesc    "1 портфель · 10 бумаг · без AI/PDF/уведомлений"
plan.card.proUnlocked "Все возможности разблокированы" / "All features unlocked"
plan.card.getCta      "⭐ Получить Pro →" / "⭐ Get Pro →"
plan.card.expiry      "до" / "until"
plan.card.unlimited   "Бессрочно" / "Unlimited"
```

**Admin:**
```
adm.colPlan / adm.sourcesTitle / adm.clearRatingsCache
adm.roleSuperAdmin / adm.roleUser
adm.yookassaTitle / adm.yookassaStub / adm.yookassaConfigured
adm.yookassaSave / adm.yookassaSaved / adm.yookassaError / adm.yookassaKeyStored
adm.planSetProUnlim / adm.planSetPro30d / adm.planSetFree / adm.planExtend30d
```

**Notifications:**
```
notif.proBanner "🔒 Telegram-уведомления доступны в Pro-тарифе" / "🔒 Telegram notifications available in Pro"
notif.getProBtn "Получить Pro" / "Get Pro"
```

**Misc:**
```
auth.sessionExpired  "Сессия истекла. Пожалуйста, войдите заново." / "Session expired. Please log in again."
err.networkError     "Ошибка сети" / "Network error"
err.requestError     "Ошибка запроса" / "Request error"
err.saveFailed       "Ошибка сохранения" / "Save failed"
```

### 2.2 HTML изменения

Заменить hardcoded Russian в upgrade-modal — добавить `data-i18n=` атрибуты.
Заменить в admin panel `<th>Тариф</th>` → `<th data-i18n="adm.colPlan">Тариф</th>`.
Заменить hardcoded `"Источники данных"` и другие admin заголовки.

### 2.3 JS изменения

В `renderPlanCard()` — использовать `t('plan.card.*')`.
В `renderNotifProBanner()` — использовать `t('notif.proBanner')`, `t('notif.getProBtn')`.
В `adminLoadUsers()` — использовать `t('adm.roleSuperAdmin')`, `t('adm.roleUser')`, `t('adm.planSetProUnlim')` и т.д.
В `adminSaveYooKassa()` — использовать `t('adm.yookassaSaved')`, `t('adm.yookassaError')`.
В `doCheckout()` — использовать `t('upgrade.stubMsg')`.
При ошибке в `apiFetch` — использовать `t('auth.sessionExpired')`.

После вызова `setLang()` — убедиться что `applyI18n()` обновляет атрибуты в upgrade-modal (modal уже в DOM).

---

## Phase 3 — Тесты

**Файл:** `tests/test_api.py`

### Новые тест-кейсы (8 штук):

```python
# 1. Free plan: 11th instrument → 403
async def test_free_plan_instrument_limit(client, test_auth_token)

# 2. Free plan: 2nd portfolio → 403
async def test_free_plan_portfolio_limit(client, test_auth_token)

# 3. Free plan: PDF → 403
async def test_free_plan_pdf_blocked(client, test_auth_token)

# 4. Admin sets plan, returns 200
async def test_admin_set_plan(client, admin_token)

# 5. Pro plan: PDF accessible after admin upgrade
async def test_pro_plan_pdf_accessible(client, test_auth_token, admin_token)

# 6. Plan expiry: expired Pro → 403 on PDF
async def test_expired_pro_plan_reverts_to_free(client, test_auth_token, admin_token)

# 7. Payment stub: returns stub response
async def test_payment_checkout_stub(client, test_auth_token)

# 8. IDOR: user cannot access another user's portfolio
async def test_idor_portfolio_access(client)
```

Вспомогательные фикстуры в `tests/conftest.py`:
- `admin_token` — JWT токен для пользователя-администратора (он уже создаётся — использовать существующий `test_auth_token` который уже admin)
- `second_user_token` — новый пользователь для IDOR теста

---

## Phase 4 — UX

### 4.1 ARIA — минимальный набор
`app/ui/dashboard.html`:
- `.modal-overlay` → добавить `role="dialog"` и `aria-modal="true"`
- `#_toast_notification` или аналогичный toast → `role="status"` + `aria-live="polite"`
- Кнопки-иконки без текста → `aria-label=`
- `.status.error` → `role="alert"`

### 4.2 Toast вместо alert()
Найти все `alert(...)` (~10 мест) и заменить на `toast(msg, 'error')`.
Проверить что функция `toast()` существует и работает с двумя аргументами (цвет/тип).

### 4.3 i18n при смене языка для modal
Убедиться что после вызова `setLang()` → `applyI18n()` проходит по всем элементам в DOM включая upgrade-modal (который уже вставлен в DOM статически).

---

## Implementation Order

1. Phase 1.1–1.6 (config, auth, headers, global handler, share rate limit) — ~30 мин
2. Phase 1.7 (reset codes в SQLite) — ~45 мин
3. Phase 1.8–1.9 (admin password stderr, DB indices) — ~15 мин
4. Phase 3 (тесты) — ~60 мин, пишем сразу после реализации
5. Phase 2 (i18n — dashboard.html) — ~90 мин
6. Phase 4 (UX — ARIA, toast) — ~30 мин

---

## Verification

```bash
# 1. Все тесты
MVP_JWT_SECRET=dev_secret_key_12345_dev_secret_key_12345 .venv/bin/python -m pytest tests/ -v

# 2. Короткий JWT секрет → ошибка валидации
MVP_JWT_SECRET=short .venv/bin/uvicorn app.main:app --reload
# Ожидается: ValidationError при запуске

# 3. Security headers
curl -I http://localhost:8000/app | grep -E "Content-Security|Strict-Transport"

# 4. Share rate limit
for i in $(seq 1 35); do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/share/test/table; done
# Ожидается: 429 после 30 запросов

# 5. i18n переключение
# Открыть /app, открыть Settings→Account, сменить язык EN→RU
# Проверить что Pro card переводится

# 6. Добавить 11й инструмент на Free аккаунте → 403 → upgrade modal

# 7. Reset code в SQLite
sqlite3 data/portfolio.db "SELECT * FROM password_reset_codes;"
```

---

## Что НЕ делаем (вне скоупа MVP)

- Token revocation blacklist — для MVP JWT expiry достаточно
- CSRF tokens — Bearer JWT не уязвим к CSRF
- Шифрование Telegram/YooKassa токенов в БД — env vars достаточно для MVP
- Pagination/virtualization таблицы — отдельный тикет
- Полное ARIA покрытие — только критичный минимум
- Password complexity rules — пользователь выбрал мягкую политику
