# Отображение значка валюты для валютных облигаций

## Контекст

FX-конвертация уже реализована: `BondSnapshot` содержит `face_unit` (SUR/CNY/USD/EUR/CHF), но это поле не пробрасывается в `InstrumentMetrics` и не отображается на фронтенде. Пользователь хочет видеть валюту номинала облигации в таблице портфеля.

## Изменения

### 1. `app/models.py` — добавить `face_unit` в InstrumentMetrics (строка ~58)

```python
face_unit: str | None = None  # Валюта номинала (SUR, CNY, USD, EUR, CHF)
```

### 2. `app/services/portfolio_service.py` — пробросить face_unit (строка ~346)

В `fetch_row()` при создании `InstrumentMetrics` для облигаций добавить:
```python
face_unit=snapshot.face_unit,
```

### 3. `app/ui/dashboard.html` — бейдж валюты (строка ~3817)

После тикер-бейджа, рядом с КИ-бейджем, добавить бейдж валюты для облигаций с `face_unit != "SUR"`:

```javascript
if (row.face_unit && row.face_unit !== 'SUR') {
    const fxBadge = document.createElement('span');
    fxBadge.textContent = {CNY:'¥ CNY', USD:'$ USD', EUR:'€ EUR', CHF:'₣ CHF'}[row.face_unit] || row.face_unit;
    fxBadge.title = 'Номинал в ' + row.face_unit;
    fxBadge.style.cssText = '...';  // стиль как у КИ-бейджа но синий
    nameTd.appendChild(fxBadge);
}
```

Вставить после строки 3818 (после `tickerBadge`), перед блоком `is_qual` (строка 3819).

## Верификация

1. Добавить юаневую облигацию (например RU000A10ECW0) в портфель
2. Проверить что в таблице рядом с тикером виден бейдж `¥ CNY`
3. Рублёвые облигации — бейдж НЕ показывается
4. `pytest` — все тесты проходят
