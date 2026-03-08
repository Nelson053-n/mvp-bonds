# MVP Bonds — портфель облигаций и акций

Веб-приложение для учёта инвестиционного портфеля на Московской бирже. Данные берутся из MOEX ISS API, хранятся в SQLite, UI — одностраничный дашборд.

## Возможности

- **Акции и облигации** — добавление по тикеру, автоматическое определение типа инструмента
- **Рыночные данные** — текущая цена, НКД, доходность к погашению, даты купонов, кредитный рейтинг (smart-lab + MOEX)
- **Мультипортфели** — несколько портфелей на аккаунт, переключение между ними
- **Шаринг** — публичная ссылка на портфель с опциональной защитой паролем
- **AI-комментарий** — короткий комментарий к каждой строке от LLM (stub или OpenAI-совместимый)
- **Аутентификация** — JWT + bcrypt, первый запуск создаёт admin-пользователя

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

MVP_JWT_SECRET=your_secret_key uvicorn app.main:app --reload
```

Дашборд: http://localhost:8000

При первом запуске в логах будет выведен пароль admin-пользователя.

## Переменные окружения

Префикс `MVP_`:

| Переменная | Обязательная | По умолчанию |
|---|---|---|
| `MVP_JWT_SECRET` | Да | — |
| `MVP_SQLITE_DB_PATH` | Нет | `data/portfolio.db` |
| `MVP_LLM_MODE` | Нет | `stub` |
| `MVP_OPENAI_API_KEY` | При `llm_mode=openai` | — |
| `MVP_OPENAI_BASE_URL` | Нет | `https://api.openai.com/v1` |
| `MVP_OPENAI_MODEL` | Нет | `gpt-4o-mini` |
| `MVP_LOG_FORMAT` | Нет | `text` |

SMTP-переменные (`MVP_SMTP_*`) — опционально для email-уведомлений.

## Тесты

```bash
pytest                              # все тесты с покрытием
pytest tests/test_api.py           # один файл
pytest tests/test_api.py::test_health  # один тест
```

## Архитектура

```
app/
├── main.py                  # FastAPI, роутеры, lifespan (запуск кэша)
├── config.py                # Pydantic Settings (env vars)
├── models.py                # Pydantic-модели запросов и ответов
├── api/                     # Тонкий API-слой (auth, portfolios, bonds, settings, admin)
├── services/
│   ├── portfolio_service.py # Оркестратор (MOEX + storage + cache + LLM)
│   ├── moex_service.py      # MOEX ISS API (акции и облигации)
│   ├── storage_service.py   # SQLite (пользователи, портфели, инструменты)
│   ├── cache_service.py     # Фоновое обновление метрик в памяти
│   ├── auth_service.py      # JWT + bcrypt
│   └── llm_service.py       # AI-комментарии (stub / OpenAI)
└── ui/
    └── dashboard.html       # Весь фронтенд в одном файле (vanilla JS)
```

LLM определяет тип инструмента и формирует короткий комментарий. Все расчёты делает backend на основе данных MOEX.
