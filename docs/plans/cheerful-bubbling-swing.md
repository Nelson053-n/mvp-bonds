# Полная прибыль облигаций (НКД + купоны) — только для T-Bank-портфелей

## Контекст

Пользователь shedrinn@gmail.com пожаловался на «неверную доходность». Разбор показал:
ранее процент в шапке считался от текущих активов вместо вложенной суммы (уже
исправлено, commit 768cd0f). Но реальное расхождение глубже: наша «прибыль» по
облигации = только переоценка тела (`(clean_price − purchase_price) × qty`), тогда
как в приложении Т-Банка доходность включает **НКД и фактически полученные купоны**.
Поэтому портфель, который в Т-Банке в плюсе, у нас показывается в минусе.

Цель: показать **полную прибыль** = переоценка тела + текущий НКД + купоны,
полученные с момента покупки — как в Т-Банке.

Ключевое решение пользователя: **функционал только для T-Bank-портфелей** (где
подключён автосинк). Источник купонов и дат покупки — **журнал операций T-Bank**
(`OperationsService/GetOperations`), который мы сейчас не используем. Для ручных
портфелей колонка «Прибыль» остаётся без изменений (старый тоггл total/day).
Журнал тянем при синке и храним в БД — рендер таблицы читает из БД, не бьёт по API.

Это намеренно узкий scope: НЕ трогаем MOEX bondization, НЕ добавляем
`purchase_date` в `portfolio_items`, НЕ чиним мёртвый `realized_coupons` в
analytics-extra. Один источник, один путь расчёта.

## Что меняем

### 1. T-Bank: запрос журнала операций — `app/services/tbank_service.py`

Новый метод `get_operations(account_id, from_date)`:
- POST на `{_BASE}/tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations`
  с телом `{"accountId": ..., "from": from_date, "to": <now RFC3339>, "state": "OPERATION_STATE_EXECUTED"}`.
- `from_date` — RFC3339. На первом синке берём широкий диапазон (напр. 5 лет назад);
  на последующих — от `last_operations_sync_at` (см. п.3), чтобы тянуть только новое.
- Парсинг `operations[]`: для каждой операции вернуть `{figi, date, operation_type, payment}`,
  где `payment = _money_value(op["payment"])` (MoneyValue units/nano, знаковый).
- Переиспользовать `_check_response`, `_money_value` (уже есть, строки 35-67).
- Пагинации у `GetOperations` нет — один запрос на диапазон.

Новый статический хелпер `_operations_to_coupons_and_buys(operations)`:
- Суммирует `payment` по `figi` для `operation_type == "OPERATION_TYPE_COUPON"` → купоны (₽, положительные).
- Находит минимальную `date` среди `OPERATION_TYPE_BUY`/`OPERATION_TYPE_BUY_CARD` по `figi` → дата первой покупки (для отображения; для купонов не требуется).
- Возвращает `{figi: {"coupons": float, "first_buy": str|None}}`.

Связка с позицией — по `figi`. Позиции из `GetPortfolio` уже содержат `figi`
(`pos["figi"]`), но `_positions_to_items` его отбрасывает (строки 144-150). Нужно
**добавить `figi` в items** там, чтобы при вставке/синке знать figi позиции.

### 2. Интеграция в синк — `tbank_service.sync_portfolio` (строки 163-265)

После обработки позиций:
- Вызвать `get_operations(account_id, from_date)`, агрегировать через
  `_operations_to_coupons_and_buys`.
- Сохранить агрегат в новую таблицу `tbank_coupons` (см. п.3) через
  `storage.upsert_tbank_coupons(portfolio_id, figi, coupons_total, first_buy)`.
- Обновить `last_operations_sync_at` на sync-строке.
- Дешёвая ошибка журнала не должна валить синк позиций — обернуть в try/except с логом
  (как `update_sync_cash`, строки 192-196).

`_positions_to_items` (строка 121): добавить `"figi": pos.get("figi")` в item-словарь,
прокинуть `figi` в `storage.add_item`/`update_item` (новый необязательный параметр).

### 3. Хранение — `app/services/storage_service.py` + `app/services/storage/items.py`

Новая таблица (в `_ensure_db`, рядом с `portfolio_sync` ~строка 246, паттерн `IF NOT EXISTS`):
```sql
CREATE TABLE IF NOT EXISTS tbank_coupons (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    figi TEXT NOT NULL,
    coupons_total REAL NOT NULL DEFAULT 0,
    first_buy_date TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, figi)
)
```
Методы в storage:
- `upsert_tbank_coupons(portfolio_id, figi, coupons_total, first_buy_date)` —
  INSERT ... ON CONFLICT(portfolio_id, figi) DO UPDATE.
- `get_tbank_coupons(portfolio_id) -> dict[figi -> {coupons_total, first_buy_date}]`.

Миграции для `portfolio_items.figi` и `portfolio_sync.last_operations_sync_at` —
паттерн `ALTER TABLE ... ADD COLUMN` в try/except `OperationalError` (строки 93-106
и рядом с sync-конфигом). `items.add_item`/`update_item`/`get_items` (в
`storage/items.py`): добавить необязательный `figi`, включить в INSERT/SELECT/возврат.

### 4. Расчёт полной прибыли — `app/services/portfolio_service.py`

В `get_table_fresh` (строка 316): до gather один раз подгрузить
`coupons_map = storage_service.get_tbank_coupons(portfolio_id)` (пусто для ручных
портфелей — тогда полный режим не активируется).

`PortfolioItem` (dataclass ~31-55): добавить поле `figi: str | None`, читать в `from_dict`.

В bond-ветке `fetch_row` (328-407):
```python
realized = coupons_map.get(item.figi, {}).get("coupons_total", 0.0) if item.figi else 0.0
aci_component = (snapshot.aci or 0.0) * item.quantity
full_profit = profit + aci_component + realized
```
Передать в `InstrumentMetrics`: `realized_coupons=round(realized, 2)`,
`full_profit=round(full_profit, 2)`. НКД-компонент уже в RUB (`snapshot.aci`);
купоны из журнала уже в RUB. Купоны хранятся на уровне позиции (суммарно по figi),
поэтому на `quantity` НЕ умножаем.

`app/models.py` `InstrumentMetrics` (рядом с `aci`, строка 62): новые опциональные поля
`realized_coupons: float | None = None`, `full_profit: float | None = None`.
Стоки и error-row их не заполняют (остаются None).

Признак «портфель поддерживает полный режим» — наличие непустого `coupons_map`.
Прокинуть на фронт флаг (напр. в ответе table или отдельным полем строки): проще
всего — если у строки `full_profit is not None`, фронт показывает полный режим.

### 5. Фронт — 3-режимный тоггл — `app/ui/dashboard.html`

Тоггл активен **только если в таблице есть строки с `full_profit != null`** (T-Bank-портфель).
Иначе колонка работает как сейчас (total/day).

- `window._profitMode` (строка 2523): значения `'full'|'total'|'day'`, default `'full'`
  если портфель поддерживает полный режим, иначе `'total'`. Валидировать localStorage.
- `setProfitMode` (2524-2544): принять 3 значения, label + стиль кнопки по режиму.
  Метки из новых ключей TRANSLATIONS (`tbl.profitFull`/`tbl.profitTotal`/`tbl.profitDay`).
- Обработчик тоггла (5784-5791): цикл `['full','total','day']` (для ручных — `['total','day']`).
- Рендер ячейки (5634-5650): `full` → `row.full_profit ?? row.profit`; `total` → `row.profit`;
  `day` → `row.day_profit`. Фон строки (5651-5653) оставить на `row.profit`.
- `getSortValue` (2545-2552): добавить ветку `full` → `row.full_profit ?? row.profit`.
- Спец-обработка sort-arrow для th-profit (5805-5815) — не трогаем, она не переписывает
  innerHTML, переживёт смену label.
- `TH_TIPS.profit` (~10668): описать 3 режима (Полная / От покупки / За день).
- Локализация ru (~9363) и en (~9765): новые ключи меток режимов.

### 6. Шапка stat-profit — `app/ui/dashboard.html`

- `calculateSummary` (2592): добавить `totalFullProfit = Σ(row.full_profit ?? row.profit)`
  и `totalRealizedCoupons = Σ(row.realized_coupons || 0)`.
- `updateStatCards` (2666-2695): для T-Bank-портфеля headline = `totalFullProfit`,
  процент = `totalFullProfit / totalInvested * 100`. Подстроку breakdown «купоны: …»
  взять из `summary.totalRealizedCoupons` (вместо мёртвого `_analyticsExtra.realized_coupons`),
  «НКД: …» оставить. Для ручных — поведение как сейчас (`totalProfit`).
- Перерисовка плитки при смене тоггла: обработчик уже зовёт `renderTable()`; убедиться,
  что следом вызывается `updateStatCards`.

### 7. Service worker

`app/ui/sw.js` строка 1: `bond-ai-v46` → `bond-ai-v47`.

## Файлы

- `app/services/tbank_service.py` — `get_operations`, `_operations_to_coupons_and_buys`, figi в items, интеграция в `sync_portfolio`
- `app/services/storage_service.py` — таблица `tbank_coupons`, upsert/get, миграции figi + last_operations_sync_at
- `app/services/storage/items.py` — `figi` в add_item/update_item/get_items
- `app/services/portfolio_service.py` — `PortfolioItem.figi`, coupons_map, full_profit в fetch_row
- `app/models.py` — `realized_coupons`, `full_profit` в InstrumentMetrics
- `app/ui/dashboard.html` — 3-режимный тоггл, рендер, сортировка, тултип, i18n, шапка
- `app/ui/sw.js` — bump кэша

## Не входит (явно)

- MOEX bondization для ручных портфелей (купоны там не учитываем — только НКД, если позже понадобится)
- `purchase_date` в `portfolio_items` (дату берём из журнала, только для отображения)
- Починка `realized_coupons` SUM(amount) в analytics-extra (мёртвый код, оставить как есть, можно пометить комментом)
- Историческая FX-конверсия купонов (журнал T-Bank уже в рублях по факту операции)

## Проверка

1. **Unit:** новый тест `tests/test_tbank_service.py::test_operations_to_coupons` —
   фикстура операций (BUY + COUPON по двум figi), проверить суммы купонов по figi и
   first_buy = минимальная дата BUY. Тест `get_operations` с monkeypatch httpx-ответа.
2. **Storage:** тест upsert/get `tbank_coupons` (round-trip, ON CONFLICT перезапись).
3. **Прод-проверка для shedrinn@gmail.com** (портфели 78, 79 — T-Bank):
   - После деплоя запустить синк (или дождаться авто) → проверить, что `tbank_coupons`
     заполнилась: `get_tbank_coupons(78)` непустой.
   - Через `portfolio_service.get_table(78)` сравнить: `full_profit` по бумагам =
     `profit + aci*qty + купоны`, суммарная полная прибыль и % приблизились к Т-Банку
     (ожидаем сдвиг из минуса в районе −2.27% в сторону плюса).
   - Скриншот колонки через Playwright: тоггл переключает 3 режима, тултип корректен.
4. **Регрессия ручных портфелей:** портфель без синка — тоггл остаётся total/day,
   `full_profit` null, ошибок в консоли нет.
5. `pytest` — существующие тесты зелёные (новые поля опциональны/аддитивны).
