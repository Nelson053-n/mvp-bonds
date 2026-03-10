# Plan: UI/UX Polish + Rating Schedule + Recommendations Fix

## Context

Серия точечных исправлений и улучшений UX по итогам ревью:
- Рейтинги обновлять раз в сутки (сейчас — один раз при старте сервиса, повторно не запрашиваются)
- Рекомендации: не предлагать выпуски одного эмитента (один эмитент — 10 серий → нет диверсификации)
- История стоимости: нулевые данные — рисовать нулевую линию, подпись "данные накапливаются" снизу по центру
- Пироги в тёмной теме: donut hole = белый (#fff захардкожен) → заменить на `--bg-card`
- SWOT: выводить по 2 пункта из разных областей, не дублировать
- Плашки KPI: `stat-sub` (`--text-muted: #475569`) плохо читается → светлее (`#64748b`)
- Плашка «Следующий купон» → добавить в sub: эмитент + сумма купона
- «Обновлено ЧЧ:ММ» и «↺ Обновить» → перенести в topbar после nav-кнопок; добавить имя портфеля
- Настройки → Аккаунт: желтый info-box с хардкодными светлыми цветами → theme-aware стиль
- Настройки → Портфели: кнопки «Переименовать», «Поделиться» → тёмный стиль для тёмной темы
- Лучшие/слабые позиции: `border-bottom:var(--slate-100)` = белые полоски → `var(--table-border)`
- Купонные уведомления: переместить из отдельной карточки в «Настройки бота» после порога просадки
- UX: empty state для пустого портфеля, горячая клавиша R, copy тикера по клику

---

## Critical Files

| Файл | Что меняем |
|------|-----------|
| `app/main.py` | Добавить `_rating_refresh_loop()` background task |
| `app/services/storage_service.py` | Добавить `get_all_portfolio_items_for_rating()` |
| `app/services/moex_service.py` | Добавить метод `refresh_rating(secid)` |
| `app/ui/dashboard.html` | Все визуальные + логические правки (CSS + JS + HTML) |

---

## Phase 1 — Ежесуточное обновление рейтингов

### 1.1 `storage_service.py` — `get_all_portfolio_items_for_rating()`
Возвращает один item на уникальный тикер (для избежания дублей):
```python
def get_all_portfolio_items_for_rating(self) -> list[dict]:
    with self._connect() as conn:
        rows = conn.execute("""
            SELECT MIN(id) as id, MIN(portfolio_id) as portfolio_id, ticker, instrument_type
            FROM portfolio_items
            GROUP BY ticker
            ORDER BY ticker
        """).fetchall()
    return [{"id": r[0], "portfolio_id": r[1], "ticker": r[2], "instrument_type": r[3]} for r in rows]
```

### 1.2 `moex_service.py` — `refresh_rating(secid)`
Сбрасывает кэш и принудительно перезапрашивает:
```python
async def refresh_rating(self, secid: str) -> str | None:
    """Force re-fetch rating, bypassing in-memory cache."""
    self._credit_rating_cache.pop(secid, None)
    self._credit_rating_cache.pop(f"smartlab:{secid}", None)
    rating = await self._get_smartlab_credit_rating(secid)
    if rating is None:
        rating = await self._get_credit_rating(secid)
    return rating
```

### 1.3 `main.py` — `_rating_refresh_loop()`
Запускать в 03:00 UTC ежесуточно (offset от текущего времени):
```python
async def _rating_refresh_loop():
    from datetime import datetime, timezone
    from app.services.moex_service import moex_service
    while True:
        now = datetime.now(timezone.utc)
        secs_to_3am = ((3 - now.hour) % 24) * 3600 - now.minute * 60 - now.second
        if secs_to_3am <= 0:
            secs_to_3am += 86400
        await asyncio.sleep(secs_to_3am)
        try:
            items = storage_service.get_all_portfolio_items_for_rating()
            sem = asyncio.Semaphore(3)
            async def _one(item):
                async with sem:
                    try:
                        rating = await moex_service.refresh_rating(item["ticker"])
                        if rating is not None:
                            storage_service.update_rating(item["id"], item["portfolio_id"], rating)
                    except Exception:
                        pass
            await asyncio.gather(*(_one(i) for i in items))
            logger.info("Daily rating refresh: %d tickers", len(items))
        except Exception:
            logger.exception("Daily rating refresh failed")
```
Добавить `asyncio.create_task(_rating_refresh_loop())` в lifespan.

---

## Phase 2 — Рекомендации: один эмитент — одна карточка

### `loadPortfolioRecommendations()` в dashboard.html

Добавить функцию `_issuerKey(bond)` и фильтрацию по уже показанным эмитентам:

```js
function _issuerKey(bond) {
  const t = (bond.ticker || '').toUpperCase();
  // Для ISIN-формата (RU000A...) — берём первые 2 слова имени
  if (/^RU\d{9,}/.test(t)) {
    return (bond.name || bond.short_name || t).split(' ').slice(0, 2).join(' ').toUpperCase().substring(0, 16);
  }
  // Для именных тикеров (POLYP-1, POLYP-2) — убираем суффикс-серию
  return t.replace(/[-_]?\d+[A-Z]*$/, '').substring(0, 8);
}

// При сборке cards:
const shownIssuers = new Set();
results.forEach((res, i) => {
  if (res.status !== 'fulfilled') return;
  const items = (res.value.bonds || res.value.suggestions || [])
    .filter(s => !existingTickers.has(s.ticker));
  // Найти первый bond от нового эмитента
  const item = items.find(s => !shownIssuers.has(_issuerKey(s)));
  if (!item) return;
  shownIssuers.add(_issuerKey(item));
  cards.push({ ...item, rationale: top4[i].rationale, risk: top4[i].risk });
});
```

---

## Phase 3 — История стоимости: нулевые данные

### `drawHistoryChartEmpty()` — фикс центровки + нулевая линия

Проблема: `canvas.offsetWidth` возвращает 0 до рендера, текст смещается. Решение — использовать `canvas.parentElement.clientWidth`.

```js
function drawHistoryChartEmpty(canvas) {
  if (!canvas) return;
  const parent = canvas.parentElement;
  const W = (parent ? parent.clientWidth : 0) || 600;
  const H = 200;
  canvas.width = W;
  canvas.height = H;
  canvas.style.width = '100%';
  // ... grid + dashed line
  const ctx = canvas.getContext('2d');
  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = labelClr;
  ctx.font = '600 12px Inter,sans-serif';
  ctx.fillText('История накапливается', W / 2, H - pad.b - 8);  // ← снизу
  ctx.font = '11px Inter,sans-serif';
  ctx.fillStyle = mutedClr;
  ctx.fillText('Данные появятся через сутки после первого запуска', W / 2, H - pad.b + 8);
  // Нулевая Y-метка
  ctx.textAlign = 'right';
  ctx.font = '10px Inter,sans-serif';
  ctx.fillStyle = labelClr;
  ctx.fillText('0', pad.l - 4, pad.t + chartH + 3);
  ctx.restore();
}
```

---

## Phase 4 — CSS / Visual Fixes (dashboard.html)

### 4.1 Donut hole — динамический цвет
В `drawPieChart`:
```js
ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg-card').trim() || '#0d1829';
```

### 4.2 stat-sub светлее
```css
/* В :root (тёмная тема) */
--text-muted: #64748b;  /* было #475569 */
```

### 4.3 info-box-warning theme-aware
Добавить CSS класс `.info-box-warning`:
```css
.info-box-warning {
  background: rgba(251,191,36,.08);
  border-color: rgba(251,191,36,.25);
  color: var(--amber-400);
}
[data-theme="light"] .info-box-warning {
  background: #fefce8;
  border-color: #fde047;
  color: #854d0e;
}
```
В HTML строка ~1149: заменить inline стили на `class="info-box info-box-warning"`.

### 4.4 Кнопки портфелей — тёмный стиль
В `settingsLoadPortfolios()` JS (строки ~5527-5561):
```js
// editBtn и shareBtn:
btn.style.cssText = '...color:var(--text-secondary);background:var(--bg-elevated);border:1px solid var(--border);...';
// delBtn:
delBtn.style.cssText = '...color:var(--red-400);background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.2);...';
```

### 4.5 Лучшие/слабые позиции — стиль строк
В `renderPerformers()`:
```js
row.style.cssText = '...border-bottom:1px solid var(--table-border);...';
name.style.cssText = '...color:var(--text-primary);';
ticker.style.cssText = '...color:var(--text-muted);';
right.style.cssText = `...color:${r.profit >= 0 ? 'var(--green-400)' : 'var(--red-400)'};`;
```

---

## Phase 5 — Topbar: имя портфеля + Обновлено + Обновить

### Структура изменений

**Удалить из HTML** блок строки 949-958 (контейнер с `last-update-time` и `btn-refresh`).

**В `setupPortfolioSelector()`** после создания селектора добавить в `topbar-right`:
```js
// Имя портфеля
const nameSpan = document.createElement('span');
nameSpan.id = 'topbar-portfolio-name';
nameSpan.style.cssText = 'font-size:12px;font-weight:500;color:var(--text-secondary);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-right:1px solid var(--border);padding-right:10px;';

// Обновлено
const timeSpan = document.createElement('span');
timeSpan.id = 'last-update-time';
timeSpan.style.cssText = 'font-size:11px;color:var(--text-muted);';

// Кнопка Обновить
// Переместить существующий #btn-refresh или воссоздать
```

**В `updatePortfolioSelector()`** обновлять имя:
```js
const nameEl = document.getElementById('topbar-portfolio-name');
if (nameEl) nameEl.textContent = portfolios.find(p => p.id === portfolioId)?.name || '';
```

**Убрать** блок `justify-content:space-between` обёртки над таблицей, оставить только `<div id="table-status">`.

---

## Phase 6 — «Следующий купон»: эмитент + сумма

В функции `renderTable()` или `updateStatCards()` (где заполняется `stat-next-coupon`):

```js
const nextRow = [...tableRows]
  .filter(r => r.next_coupon_date)
  .sort((a, b) => new Date(a.next_coupon_date) - new Date(b.next_coupon_date))[0];
if (nextRow) {
  const amount = nextRow.coupon != null ? Math.round(nextRow.coupon * nextRow.quantity) : null;
  const emitter = (nextRow.name || nextRow.ticker || '').substring(0, 20);
  const sub = document.getElementById('stat-next-coupon-sub');
  if (sub) sub.textContent = amount ? `${emitter} · ${amount.toLocaleString('ru')} ₽` : emitter;
}
```

---

## Phase 7 — SWOT: 2 из разных областей, без дублей

В `renderSwotAnalysis(rows)` добавить тег категории, выбирать `pickDiversified(items, 2)`:

```js
// Категории: 'ytm', 'coupon', 'ofz', 'concentration_ticker', 'concentration_issuer',
//            'maturity_cluster', 'maturity_short', 'maturity_long', 'offer', 'losses', 'pl', 'size', 'rating'
function pickDiversified(tagged, maxCount) {
  const result = [], used = new Set();
  for (const item of tagged) {
    if (result.length >= maxCount) break;
    if (!used.has(item.category)) { result.push(item.text); used.add(item.category); }
  }
  // Добрать если не хватает уникальных
  for (const item of tagged) {
    if (result.length >= maxCount) break;
    if (!result.includes(item.text)) result.push(item.text);
  }
  return result;
}
```

---

## Phase 8 — Купонные уведомления: встроить в карточку бота

**HTML:** Убрать отдельную карточку `#settings-notifications-personal`. Добавить поля купона прямо в `card-body` карточки «Настройки бота» — после поля `tg-threshold`, перед кнопками сохранения:

```html
<!-- После tg-threshold field, перед notif-actions -->
<div class="field">
  <label data-i18n="settings.coupon_notif_title">Уведомления о купонах</label>
  <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:4px;">
    <input type="checkbox" id="coupon-notif-enabled">
    <span style="font-size:13px;" data-i18n="settings.coupon_notif_enable">Включить</span>
  </label>
  <div id="coupon-notif-days-row" style="display:none;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;">
    <span style="font-size:12px;color:var(--text-muted);" data-i18n="settings.coupon_notif_days">За дней:</span>
    <input type="number" id="coupon-notif-days" min="1" max="30" value="3" class="input" style="width:56px;" />
    <button id="btn-save-coupon-notif" class="btn btn-sm" data-i18n="settings.save">Сохранить</button>
  </div>
  <p id="coupon-notif-hint-tg" class="settings-hint" style="display:none;color:var(--amber-400);" data-i18n="settings.coupon_notif_no_tg">Нужен Chat ID в разделе «Аккаунт»</p>
  <div id="coupon-notif-status" class="status"></div>
</div>
```

---

## Phase 9 — UX улучшения

### 9.1 Empty state для пустого портфеля
После таблицы (внутри `.table-wrap` или снаружи) добавить hidden div:
```html
<div id="empty-portfolio-state" style="display:none;text-align:center;padding:60px 20px;">
  <svg .../>  <!-- иконка портфеля -->
  <div style="font-size:16px;font-weight:600;...">Портфель пуст</div>
  <div style="font-size:13px;color:var(--text-muted);margin:6px 0 20px;">
    Добавьте первую бумагу для отслеживания доходности
  </div>
  <button class="btn" onclick="openAddInstrumentModal()">+ Добавить бумагу</button>
</div>
```
В `renderTable()`: `emptyState.style.display = tableRows.length ? 'none' : 'block'`.

### 9.2 Копирование тикера по клику
В ячейке тикера таблицы добавить `cursor:pointer; title="Нажмите чтобы скопировать"` и onclick:
```js
tickerEl.style.cursor = 'pointer';
tickerEl.title = 'Скопировать тикер';
tickerEl.onclick = () => {
  navigator.clipboard.writeText(row.ticker).then(() => showToast(`Скопировано: ${row.ticker}`));
};
```
Добавить функцию `showToast(msg)` — небольшой popup снизу экрана (1.5с).

### 9.3 Горячая клавиша R
```js
document.addEventListener('keydown', e => {
  if (e.key.toLowerCase() === 'r' && !e.ctrlKey && !e.metaKey && !e.altKey
    && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) {
    manualRefresh();
  }
});
```

---

## Implementation Priority Order

1. Phase 4 (CSS fixes: donut, stat-sub, кнопки, полосы) — самые видимые баги
2. Phase 7 (SWOT лимит + категории)
3. Phase 6 (Следующий купон: эмитент + сумма)
4. Phase 5 (Topbar: имя + Обновлено + кнопка)
5. Phase 3 (History chart: центровка + нулевая линия)
6. Phase 8 (Купонные уведомления в карточку бота)
7. Phase 4.3 (info-box-warning theme-aware)
8. Phase 2 (Рекомендации: один эмитент)
9. Phase 1 (Rating daily refresh — backend)
10. Phase 9 (Empty state, copy ticker, hotkey R)

---

## Verification

1. Тёмная тема: пироги без белого кольца
2. stat-sub текст читаем
3. Кнопки портфелей (Переименовать, Поделиться) — тёмные
4. Лучшие/слабые: полосы `var(--table-border)`, не белые
5. SWOT: ровно 2 сильных + 2 риска, разные категории
6. «Обновлено ЧЧ:ММ» и «↺ Обновить» в topbar, рядом имя портфеля
7. «Следующий купон» sub → «ИмяЭмитента · 1 500 ₽»
8. История: нулевая линия + подпись по центру снизу (не слева)
9. Купонные уведомления внутри карточки бота
10. Рекомендации: 4 разных эмитента
11. Rating refresh loop в логах в 03:00 UTC
