# Bond AI — портфель облигаций и акций

Веб-приложение для учёта инвестиционного портфеля на Московской бирже с AI-комментариями, PDF-отчётами и freemium-монетизацией.

## Возможности

### Бесплатный тариф
- **1 портфель**, до **10 инструментов** (акции и облигации)
- Рыночные данные MOEX: текущая цена, НКД, доходность к погашению, даты купонов, кредитный рейтинг
- Добавление по тикеру с автоматическим определением типа инструмента
- Аналитика: цена купленного vs. рыночная, суммарная доходность, дюрация
- Список наблюдения (Watchlist)
- Экспорт/импорт позиций в CSV

### Pro-тариф
- **Неограниченное** количество портфелей и инструментов
- **AI-комментарий** к каждой строке (stub или OpenAI-совместимый API)
- **PDF-отчёт** с полной аналитикой портфеля
- **Telegram-уведомления** — купонные выплаты и ценовые алерты
- **Шаринг** — публичная ссылка на портфель с опциональной защитой паролем

### Безопасность
- JWT + bcrypt аутентификация (токены 72 ч, секрет ≥ 32 символов)
- IDOR-защита на всех эндпоинтах портфелей
- CSP + HSTS + security headers
- Rate limiting на публичных share-эндпоинтах
- XSS-защита через HTML-экранирование пользовательских данных
- Пароли сброса хранятся в SQLite (не в памяти), очистка просроченных кодов

---

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

MVP_JWT_SECRET=your_secret_at_least_32_chars_long .venv/bin/uvicorn app.main:app --reload
```

Дашборд: http://localhost:8000/app

При первом запуске пароль admin-пользователя выводится в **stderr**.

---

## Переменные окружения

Префикс `MVP_`:

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `MVP_JWT_SECRET` | **Да** | — | Секрет для подписи JWT, минимум 32 символа |
| `MVP_SQLITE_DB_PATH` | Нет | `data/portfolio.db` | Путь к базе данных |
| `MVP_LLM_MODE` | Нет | `stub` | `stub` или `openai` |
| `MVP_OPENAI_API_KEY` | При `llm_mode=openai` | — | API-ключ OpenAI |
| `MVP_OPENAI_BASE_URL` | Нет | `https://api.openai.com/v1` | Base URL (совместимый с OpenAI) |
| `MVP_OPENAI_MODEL` | Нет | `gpt-4o-mini` | Модель |
| `MVP_LOG_LEVEL` | Нет | `INFO` | Уровень логирования |
| `MVP_LOG_FORMAT` | Нет | `json` | `json` или `text` |

SMTP-переменные (`MVP_SMTP_HOST`, `MVP_SMTP_PORT`, `MVP_SMTP_USER`, `MVP_SMTP_PASSWORD`, `MVP_SMTP_FROM`) — опционально для email-уведомлений.

---

## Монетизация (freemium)

Лимиты бесплатного тарифа проверяются на бэкенде (HTTP 403 + код ошибки):

| Лимит | Код ошибки |
|---|---|
| 2-й портфель | `FREE_LIMIT_PORTFOLIOS` |
| 11-й инструмент | `FREE_LIMIT_INSTRUMENTS` |
| PDF-отчёт | `FREE_LIMIT_PDF` |
| Telegram-уведомления | `FREE_LIMIT_NOTIFICATIONS` |
| Шаринг портфеля | `FREE_LIMIT_SHARING` |

Фронтенд перехватывает эти коды и открывает upgrade-модал.

**Управление тарифами** — только через admin-панель (`PATCH /admin/users/{id}/plan`):
```json
{ "plan": "pro", "expires_at": null }       // Pro бессрочно
{ "plan": "pro", "expires_at": 1735689600 } // Pro до даты (unix timestamp)
{ "plan": "free", "expires_at": null }      // Вернуть на Free
```

**Платёжная интеграция** — `POST /payments/checkout?plan=pro` возвращает заглушку (`status: "stub"`). Для подключения реального провайдера (YooKassa) — добавить shop_id и secret_key через admin-панель.

---

## Архитектура

**Стек:** FastAPI (async) + SQLite + vanilla HTML/CSS/JS (SPA в одном файле)

```
app/
├── main.py                  # FastAPI, роутеры, lifespan, security headers, global error handler
├── config.py                # Pydantic Settings (env vars, jwt_secret min_length=32)
├── models.py                # Pydantic-модели запросов и ответов
├── api/
│   ├── auth.py              # Регистрация, логин, смена пароля/email, сброс пароля
│   ├── portfolios.py        # CRUD портфелей, шаринг
│   ├── portfolio.py         # Инструменты, таблица, AI-комментарий
│   ├── bonds.py             # Поиск облигаций, автодополнение
│   ├── pdf.py               # PDF-отчёт (только Pro)
│   ├── payments.py          # Checkout (stub / YooKassa)
│   ├── settings.py          # Настройки аккаунта, Telegram, уведомления
│   ├── watchlist.py         # Список наблюдения
│   ├── admin.py             # Статистика, пользователи, тарифы, источники данных
│   └── deps.py              # Зависимости: get_current_user, get_user_plan, get_portfolio_or_403
├── services/
│   ├── portfolio_service.py # Оркестратор (MOEX + storage + cache + LLM)
│   ├── moex_service.py      # MOEX ISS API — акции и облигации (TQCB + TQOB)
│   ├── storage_service.py   # SQLite: пользователи, портфели, тарифы, rate limits, reset codes
│   ├── cache_service.py     # Фоновое обновление метрик в памяти (интервал 120 с)
│   ├── auth_service.py      # JWT + bcrypt, reset codes через SQLite
│   ├── llm_service.py       # AI-комментарии (stub / OpenAI-совместимый)
│   └── notification_service.py # Telegram-уведомления (купоны, алерты)
└── ui/
    ├── dashboard.html       # Весь фронтенд в одном файле (vanilla JS, i18n RU/EN)
    ├── landing.html         # Лендинг (/)
    └── sw.js                # Service Worker (PWA, cache-first для статики)
```

**Цепочка fallback цен MOEX:** `LAST → LCLOSE → PREVPRICE → PREVWAPRICE → PREVLEGALCLOSEPRICE`

**i18n:** Встроенная система переводов RU/EN — объект `TRANSLATIONS` в JS, функция `t(key)`, атрибуты `data-i18n`.

---

## Тесты

```bash
.venv/bin/python -m pytest                   # все тесты с coverage
.venv/bin/python -m pytest tests/test_api.py # один файл
.venv/bin/python -m pytest -v                # verbose
```

`MVP_JWT_SECRET` устанавливается автоматически через `pyproject.toml` (плагин `pytest-env`).

Тесты покрывают: базовые API-эндпоинты, freemium-лимиты (PDF, портфели, инструменты), управление тарифами через admin, истечение Pro-подписки, payment stub, IDOR-защиту.
