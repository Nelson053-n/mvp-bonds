# Импорт портфеля из Т-Банк Инвестиции

## Контекст

Пользователь хочет импортировать портфель из Т-Банк Инвестиции по read-only API-токену. Токен получается на сайте T-Bank (`tbank.ru/invest/settings/`). При импорте создаётся новый портфель с облигациями и акциями из брокерского счёта. Токен нигде не сохраняется.

## Решения по дизайну

- **Протокол:** REST API T-Bank через `httpx` (без SDK `tinkoff-investments` — избегаем тяжёлую gRPC-зависимость)
- **Токен:** передаётся в теле запроса, не хранится в БД
- **Типы инструментов:** только `bond` и `stock` (ETF, валюты, фьючерсы пропускаются)
- **Цена покупки:** `averagePositionPrice` из T-Bank как есть (рубли, абсолютная цена)
- **Повторный импорт:** создаёт новый портфель с суффиксом `(2)`, `(3)` и т.д.
- **UX-флоу:** 2 шага — загрузить список счетов → выбрать счёт → импортировать

## Файлы

### 1. СОЗДАТЬ `app/services/tbank_service.py`

Сервис для работы с T-Bank REST API. Не singleton — создаётся с токеном на каждый запрос.

```
BASE = "https://invest-public-api.tinkoff.ru/rest"
```

**Класс `TBankService(token)`:**
- `get_accounts()` → `POST .../UsersService/GetAccounts` → `[{id, name, type}]`
- `get_positions(account_id)` → `POST .../OperationsService/GetPortfolio` → raw positions
- `resolve_ticker(client, figi)` → `POST .../InstrumentsService/GetInstrumentBy` → ticker строка
- `import_account(account_id)` → собирает всё: positions → resolve tickers параллельно (semaphore=5) → фильтр по типу → `[{ticker, instrument_type, quantity, purchase_price}]`

**Хелперы:** `_money_value(dict) → float`, `_quotation(dict) → float` (units + nano / 1e9)

**Маппинг типов:** `INSTRUMENT_TYPE_BOND → "bond"`, `INSTRUMENT_TYPE_SHARE → "stock"`, остальные пропускаются

**Ошибки:** `TBankError(message)` — для 401 ("Неверный токен"), 429 ("Лимит запросов")

### 2. СОЗДАТЬ `app/api/tbank.py`

Роутер с prefix `/tbank`, два эндпоинта:

**`POST /tbank/accounts`** — `{token}` → `{accounts: [{id, name, type}]}`
- Валидирует токен и возвращает список счетов

**`POST /tbank/import`** — `{token, account_id}` → `{portfolio_id, portfolio_name, added, errors, error_details}`
- Проверяет лимит портфелей
- Получает имя счёта для названия портфеля (`"Т-Банк: {account.name}"`)
- Если портфель с таким именем уже есть — добавляет суффикс `(2)`, `(3)` и т.д.
- Вызывает `TBankService.import_account()`
- Создаёт портфель через `storage_service.create_portfolio()`
- Вставляет позиции через `storage_service.add_item()` (как CSV-импорт, без MOEX/LLM)
- Инвалидирует кэш `cache_service.invalidate()`

### 3. ИЗМЕНИТЬ `app/main.py` (2 строки)

```python
from app.api.tbank import router as tbank_router  # добавить импорт
app.include_router(tbank_router)                    # добавить регистрацию
```

### 4. ИЗМЕНИТЬ `app/ui/dashboard.html` (3 места)

**A. HTML-карточка** — добавить в `#stab-io` после карточки "Импорт всех портфелей":
- Поле ввода токена (type=password)
- Кнопка "Загрузить счета"
- Скрытый `<select>` для выбора счёта (появляется после загрузки)
- Кнопка "Импортировать портфель" (появляется после загрузки)
- Статус-div для сообщений

**B. JS-обработчик** — после блока `import-all-btn`:
- Клик "Загрузить счета" → `POST /tbank/accounts` → заполняет `<select>`
- Клик "Импортировать" → `POST /tbank/import` → показывает результат, очищает форму, обновляет список портфелей через `loadPortfolios()`

**C. Ключи локализации** — в объекте `TRANSLATIONS` для `ru` и `en`

## Обработка ошибок

| Сценарий | Поведение |
|---|---|
| Невалидный/просроченный токен | T-Bank 401 → HTTP 400 "Неверный токен Т-Банка" |
| Rate limit | T-Bank 429 → HTTP 400 "Лимит запросов" |
| Сеть недоступна | httpx ошибка → HTTP 500 "Ошибка подключения к Т-Банк API" |
| FIGI не распознан | Позиция пропускается, логируется |
| Нет облигаций/акций в счёте | HTTP 400 "В счёте нет облигаций или акций" |
| Лимит портфелей | HTTP 400 перед созданием |

## Проверка

1. Запустить приложение: `MVP_JWT_SECRET=REDACTED .venv/bin/uvicorn app.main:app --reload --host 192.168.10.32`
2. Зайти в Настройки → Импорт/Экспорт → найти карточку "Импорт из Т-Банка"
3. Вставить read-only токен → "Загрузить счета" → выбрать счёт → "Импортировать"
4. Убедиться что создан новый портфель с бумагами
5. Повторный импорт → создаётся портфель с суффиксом (2)
6. Запустить тесты: `.venv/bin/python -m pytest`
