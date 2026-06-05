# Режим «За день» в неторговое время + итоговая строка по режиму

## Контекст

После внедрения полной прибыли ([[project_full_profit_tbank]]) пользователь
заметил: режим «За день» в колонке «Прибыль» показывает «—» / нули вне торгов.
Причина — в `moex_service` дневной prev-close заполняется **только при наличии
сегодняшнего `LAST`** (внутри сессии). Вне торгов `LAST` нет → `day_profit=None`.

Плюс новое требование: нижняя итоговая строка таблицы («Итого») должна
пересчитывать колонку «Прибыль» под текущий режим тоггла — полная / за день /
от покупки.

Цель:
1. «За день» считается и после закрытия торгов (по сегодняшнему закрытию LCLOSE
   vs вчерашнему закрытию), а если сделок сегодня не было — показывает 0 ₽.
2. Итоговая строка таблицы (col «Прибыль») следует за режимом тоггла.

## Что меняем

### 1. prev-close вне торгов — `app/services/moex_service.py`

**Облигации** (`get_bond_snapshot`, строки 346-372). Сейчас:
- `clean_price_percent` = `LAST → LCLOSE → PREVPRICE → PREVWAPRICE → PREVLEGALCLOSEPRICE`
- `prev_close_percent` заполняется только `if md_row.get("LAST") is not None`.

Переписать выбор prev-close так, чтобы он зависел от того, ОТКУДА взялась текущая цена,
и сравнивал «сегодня» с «вчера» без сравнения цены самой с собой:
- Если `LAST` есть → current=LAST (сегодня внутри сессии), prev = первый доступный из
  `PREVLEGALCLOSEPRICE, PREVPRICE, PREVWAPRICE` (вчера). (как сейчас)
- Иначе если `LCLOSE` есть → current=LCLOSE (закрытие сегодняшней сессии),
  prev = первый из `PREVLEGALCLOSEPRICE, PREVPRICE, PREVWAPRICE` (вчера). ← НОВОЕ
- Иначе (сегодня сделок не было, current взялась из PREVPRICE/PREVLEGALCLOSE) →
  `prev_close_percent = clean_price_percent` ⇒ day = 0 (решение пользователя: 0 ₽).

Реализация: вычислять prev-кандидат той же цепочкой, что и раньше, но условие входа
сменить с «LAST is not None» на «current взята из LAST или LCLOSE». Когда current из
PREV-полей — выставить prev = clean_price_percent (нулевой день). Важно: не брать
`LCLOSE` в prev-кандидаты когда current уже = LCLOSE (иначе сравнение с собой).

**Акции** (`get_stock_snapshot`, строки 253-260) — тот же паттерн:
- `current_price` = `LAST → LCLOSE`.
- prev_close: если LAST есть → LCLOSE (вчера); если current=LCLOSE → нужен «вчера»,
  но у акций в этом endpoint нет отдельного PREV-поля в md_row. Проверить sec_row на
  `PREVPRICE`/`PREVLEGALCLOSEPRICE` (как у облигаций). Если нет prev-источника отличного
  от current → prev = current (day=0). НЕ сравнивать LCLOSE с LCLOSE.

Поведение во время торгов не меняется (ветка LAST идёт первой и идентична текущей).

### 2. Итоговая строка таблицы по режиму — `app/ui/dashboard.html`

**`calculateSummary`** (≈2628): добавить `totalDayProfit = Σ row.day_profit` (по строкам
где day_profit != null; если ни у кого нет — 0). `totalFullProfit`/`totalProfit` уже есть.

**Итоговая строка** (5811-5815): col «Прибыль» выбирать по `window._profitMode`:
- `full` → `summary.totalFullProfit`
- `day`  → `summary.totalDayProfit`
- `total`→ `summary.totalProfit`
Знак/класс (sum-positive/negative) — по выбранному значению. Вынести выбор в локальную
переменную (напр. `sumProfit`), чтобы текст и класс были согласованы.

Итоговая строка перерисовывается внутри `renderTable`, а тоггл уже зовёт `renderTable`
(после фикса гонки) — значит при переключении режима итог пересчитается автоматически.

### 3. Service worker

`app/ui/sw.js`: bump `CACHE_NAME` (сейчас v50 → v51).

## Файлы

- `app/services/moex_service.py` — prev-close вне торгов (bond + stock ветки)
- `app/ui/dashboard.html` — `totalDayProfit` в calculateSummary, итоговая строка по режиму
- `app/ui/sw.js` — bump кэша

## Не входит

- Изменение логики headline-плитки stat-profit (она уже следует режиму для full;
  day-плашка отдельная — не трогаем).
- Историческое хранение prev-close (берём из текущего snapshot MOEX, TTL как есть).

## Проверка

1. **Прод (вне торгов), портфель 78 shedrinn:** через `moex_service.get_bond_snapshot`
   проверить, что `prev_close_percent` теперь НЕ None для ликвидных бумаг (есть LCLOSE),
   и что day = LCLOSE−вчера; для неликвида без сделок day=0.
2. **portfolio_service.get_table(78):** `day_profit` у большинства бумаг не None.
3. **Playwright на проде:** режим «За день» показывает рублёвые значения (не «—»),
   итоговая строка снизу в каждом режиме (full/total/day) показывает соответствующую
   сумму; переключение тоггла меняет и колонку, и итог.
4. **Регрессия во время торгов:** логика LAST-ветки не изменилась — day P&L как прежде.
5. `pytest` зелёный (моки snapshot в test_moex_service не должны сломаться — проверить
   тесты, завязанные на prev_close_price/prev_close_percent, если есть).
