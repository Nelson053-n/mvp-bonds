# Plan: PDF Fix + Portfolio Risk by Coupon Yield

## Context

Два независимых исправления:

**1. PDF переполнение таблицы**
Сумма ширин 16 колонок = 32.7 cm при доступной ширине A4 landscape ≈ 26.7 cm (297mm − 2×1.5cm margin).
Overflow = 6.0 cm — таблица не влезает, данные наезжают. Колонка «Название» шириной 5.8 cm — главный виновник.

**2. Риск портфеля = «не определён»**
Текущая логика риска (`_calc_risk_from_ratings`) работает только если у инструментов заполнено `company_rating`.
После wizard/лендинга: rating не попадает в `portfolio_items.company_rating` немедленно, поэтому `GROUP_CONCAT` возвращает NULL → риск = `"unknown"`.
Новая логика: риск по средней купонной доходности портфеля (более надёжный источник данных).

---

## Critical Files

| Файл | Что меняем |
|------|-----------|
| `app/api/pdf.py` | Уменьшить MARGIN, пересчитать col_w, Paragraph-обёртка для ячеек |
| `app/services/storage_service.py` | Новый `_calc_risk_from_coupon()` + обновить SQL в `get_portfolios_with_item_counts()` |

---

## Phase 1 — PDF Fix (`app/api/pdf.py`)

### Причина переполнения
- Текущие col_w суммируют 32.7 cm, доступно ~26.7 cm → overflow 6 cm
- Главный виновник — колонка «Название» (5.8 cm) + слишком большие margins (1.5 cm)

### Изменения

**1. Уменьшить MARGIN с 1.5 cm до 0.8 cm:**
```python
MARGIN = 0.8 * cm
```

**2. Пересчитать col_w** (сумма = 27.1 cm, доступно 28.1 cm — запас 1 cm):
```python
col_w = [
    0.5, 1.8, 3.8, 1.5,   # №, тикер, название, тип
    1.2, 1.8, 1.8, 2.2,   # кол-во, цена покупки, тек. цена, стоимость
    1.8, 1.2, 1.5,         # P&L, P&L%, рейтинг
    1.5, 1.4, 1.8, 2.0,   # ставка купона, YTM, погашение, след.купон
    1.3,                   # доля
]
```

**3. Добавить ParagraphStyle для ячеек и обернуть название:**
```python
cell_style = ParagraphStyle(
    'Cell', fontSize=7, leading=8.5, fontName=font_name, wordWrap='CJK'
)
header_cell_style = ParagraphStyle(
    'HeaderCell', fontSize=7.5, leading=9, fontName=font_bold,
    textColor=colors.white, alignment=TA_CENTER, wordWrap='CJK'
)
```

Обернуть:
- headers в `Paragraph(h, header_cell_style)`
- название в строках данных: `Paragraph(str(row.name or '')[:50], cell_style)` (убрать `[:40]`)
- обновить `inst_style`: добавить `("WORDWRAP", (0,0), (-1,-1), 1)` и уменьшить PADDING до `2`

**4. Уменьшить font body с 7.5pt до 7pt** для плотных колонок, header оставить 7.5pt.

---

## Phase 2 — Portfolio Risk by Coupon Yield (`app/services/storage_service.py`)

### Почему текущий риск = «unknown»
После wizard: `portfolio_items.company_rating` = NULL (рейтинг заполняется только при обновлении кэша и вызове `update_rating()`). `GROUP_CONCAT(NULL)` = пустая строка → `_calc_risk_from_ratings([])` → `"unknown"`.

`manual_coupon_rate` заполняется сразу при refresh кэша после добавления инструментов — это надёжный источник.

### Новый метод `_calc_risk_from_coupon(avg_coupon_rate)`
```python
@staticmethod
def _calc_risk_from_coupon(avg_coupon_rate: float) -> str:
    """Determine portfolio risk from average coupon rate (% of par)."""
    if avg_coupon_rate < 12.0:
        return "conservative"
    if avg_coupon_rate < 15.0:
        return "low"
    if avg_coupon_rate < 18.0:
        return "moderate"
    if avg_coupon_rate < 22.0:
        return "high"
    return "aggressive"
```

### Обновить SQL в `get_portfolios_with_item_counts()`

Добавить `AVG(CASE WHEN pi.manual_coupon_rate > 0 THEN pi.manual_coupon_rate END) as avg_coupon_rate`:

```sql
SELECT p.id, p.name, p.created_at,
       COUNT(pi.id) as item_count,
       COALESCE(SUM(pi.quantity * pi.purchase_price), 0) as total_cost,
       GROUP_CONCAT(pi.company_rating) as ratings,
       AVG(CASE WHEN pi.manual_coupon_rate > 0 THEN pi.manual_coupon_rate END) as avg_coupon_rate
FROM portfolios p
LEFT JOIN portfolio_items pi ON pi.portfolio_id = p.id
WHERE p.user_id = ?
GROUP BY p.id
ORDER BY p.id ASC
```

### Логика приоритета в коде:
```python
for row in rows:
    avg_coupon = row[6]   # новый столбец
    ratings_raw = row[5] or ""
    ratings = [r.strip() for r in ratings_raw.split(",") if r.strip()]

    if avg_coupon:        # приоритет: купонная доходность
        risk = self._calc_risk_from_coupon(float(avg_coupon))
    elif ratings:         # fallback: кредитные рейтинги
        risk = self._calc_risk_from_ratings(ratings)
    else:
        risk = "unknown"
```

### Меппинг для пользователя (проверить в dashboard.html)
Риск отображается в настройках-портфели. Убедиться что маппинг в JS совпадает:
- `conservative` → «Консервативный»
- `low` → «Низкий»
- `moderate` → «Умеренный»
- `high` → «Высокий»
- `aggressive` → «Агрессивный»
- `unknown` → «Не определён»

---

## Порядок реализации

1. `app/api/pdf.py` — margin + col_w + Paragraph ячейки
2. `app/services/storage_service.py` — новый метод + SQL + логика

---

## Verification

1. Запустить: `MVP_JWT_SECRET=<YOUR_JWT_SECRET> .venv/bin/uvicorn app.main:app --reload`
2. Скачать PDF портфеля → все 16 колонок влезают, нет наезда, длинные названия переносятся
3. Создать портфель через лендинг-wizard → Настройки → Мои портфели → риск показывает значение (не «Не определён»)
4. Добавить консервативную облигацию с купоном 8% → риск портфеля должен снизиться
5. Тесты: `MVP_JWT_SECRET=<YOUR_JWT_SECRET> .venv/bin/python -m pytest tests/ 2>&1 | tail -5`
