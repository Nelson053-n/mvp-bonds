# Bond AI v3 — Фаза 2: Конверсия и доверие

## Контекст

Фаза 1 завершена и закоммичена (`0854508`): favicon, /privacy, /terms, секция «Как работает AI», FAQ, дисклеймер, Яндекс.Метрика, чекбокс согласия ПД, обновлённые лимиты (3 портфеля, 15 мин).

Фаза 2 добавляет: сравнение с конкурентами, живые данные в демо, waitlist для Pro, секцию доверия со счётчиками, кастомную 404-страницу.

---

## Порядок реализации

1. **404-страница** — изолированная задача, шаблон из `share_error.html`
2. **Waitlist для Pro** — backend (таблица + endpoint) + замена кнопки Pro на email-форму
3. **Секция «Почему Bond AI»** — сравнительная таблица между Demo и «Как это работает»
4. **Секция «Доверие»** — 4 статистических карточки с анимированными счётчиками
5. **Живые данные в демо** — JS-fetch к `/bonds/search` (публичный endpoint)

---

## Задача 1: 404-страница

**Создать:** `app/ui/404.html` (~60 строк)
**Шаблон:** `app/ui/share_error.html` — тот же стиль (тёмная тема, nav, card, footer)

Контент:
- `<title>` — «Страница не найдена — Bond AI»
- Иконка: `&#128270;` (лупа)
- `<h1>` — «Страница не найдена»
- `<p>` — «Запрашиваемая страница не существует или была перемещена.»
- Кнопка «← На главную» → `/`

**Изменить:** `app/main.py`

1. Добавить path-константу (после `terms_path`, ~строка 35):
```python
not_found_path = _ui_dir / "404.html"
```

2. Добавить 404 exception handler (после generic handler, ~строка 256):
```python
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    if request.url.path.startswith(("/auth/", "/bonds/", "/portfolios/", "/pdf/",
                                     "/settings/", "/admin/", "/watchlist/", "/tbank/")):
        return JSONResponse(status_code=404, content={"detail": getattr(exc, "detail", "Not found")})
    return HTMLResponse(not_found_path.read_text(encoding="utf-8"), status_code=404)
```

API-маршруты продолжают возвращать JSON 404, браузерные — HTML-страницу.

---

## Задача 2: Waitlist для Pro

### Backend

**Изменить:** `app/services/storage_service.py`

1. Таблица в `_ensure_db()` (после `rating_history`):
```sql
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
)
```

2. Метод `add_waitlist_email(email: str) -> int` — INSERT, возвращает id. Дубликат → IntegrityError.

**Изменить:** `app/main.py`

Публичный endpoint `POST /waitlist`:
- Принимает `{email}` (JSON body)
- Валидация regex: `^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$`
- Дубликат → 200 + «Вы уже в списке ожидания!» (без user enumeration)
- Успех → 201 + «Вы в списке ожидания!»

### Frontend

**Изменить:** `app/ui/landing.html`

Заменить кнопку Pro-карточки (строка ~1416-1418) на inline email-форму:
```html
<div id="waitlist-form">
  <div style="display:flex;gap:8px;margin-bottom:8px;">
    <input type="email" id="waitlist-email" placeholder="you@example.com" ...>
    <button class="btn btn-primary" onclick="submitWaitlist()">Записаться</button>
  </div>
  <div id="waitlist-msg" style="min-height:16px;font-size:12px;"></div>
</div>
```

JS-функция `submitWaitlist()`:
- Валидация email на клиенте
- `fetch('/waitlist', {method:'POST', body: JSON.stringify({email})})`
- Успех → зелёное сообщение, disable input
- Metrika goal: `waitlist_signup`

---

## Задача 3: Секция «Почему Bond AI»

**Изменить:** `app/ui/landing.html`

**Расположение:** между Demo (конец ~строка 1334) и «Как это работает» (~строка 1337).

Сравнительная таблица (grid 4 колонки): Функция | Таблицы Excel | Другие сервисы | Bond AI

6 строк:
| Функция | Excel | Другие | Bond AI |
|---|---|---|---|
| Цены в реальном времени | Ручной ввод | Частично | MOEX каждые 15 мин |
| AI-подбор портфеля | Нет | Нет | Claude AI |
| Купонный календарь | Считать вручную | Базовый | Автоматический |
| Telegram-уведомления | Нет | Email-only | Купоны + алерты |
| Кредитные рейтинги | Искать самому | Только MOEX | SmartLab + MOEX |
| Стоимость | Лицензия Office | от 500 ₽/мес | Бесплатно |

CSS: `.comparison-section`, `.comparison-grid` (grid 4 col), `.comp-header`, `.comp-cell`, `.comp-highlight` (Bond AI столбец — синий акцент)

Responsive:
- 768px: уменьшить padding
- 640px: горизонтальный скролл (обернуть grid в overflow-x: auto контейнер)

---

## Задача 4: Секция «Доверие»

**Изменить:** `app/ui/landing.html`

**Расположение:** между «Как это работает» (конец ~строка 1366) и Pricing (~строка 1369).

4 карточки в ряд с вертикальными разделителями:

| 1200+ | 15 мин | 30 сек | Бесплатно |
|---|---|---|---|
| облигаций в базе | обновление данных | AI-подбор портфеля | навсегда |
| ОФЗ и корп. бумаги | Цены с MOEX | Claude AI анализирует | Никаких платежей |

Каждая карточка: иконка (56x56 rounded, цветная) + число + подпись + подтекст.

CSS: `.trust-section`, `.trust-stats-grid` (4 col), `.trust-stat` с разделителями `::after`.

JS: Анимация счётчиков при скролле (IntersectionObserver + requestAnimationFrame, ease-out cubic, 1.5 сек).

Responsive: 768px → 2x2, 640px → 2x2 с уменьшенным padding.

---

## Задача 5: Живые данные в демо

**Изменить:** `app/ui/landing.html`

Endpoint `/bonds/search` — **публичный** (использует `get_optional_user`, auth не обязателен).

Возвращает: `{ticker, name, coupon_percent, maturity, rating, price, is_qual}`.

Стратегия:
1. Оставить статическую таблицу как fallback (уже есть в HTML)
2. При загрузке страницы — `fetch('/bonds/search?q=SU26&limit=3')` + `fetch('/bonds/search?q=RU000A1&limit=2')`
3. При успехе — заменить `tbody` демо-таблицы динамическими данными
4. При ошибке — ничего не делать, остаётся статика
5. KPI-блок остаётся статическим (это «аспирационные» данные портфеля)

JS: IIFE `loadDemoData()` с `escapeHtml()` для XSS-защиты. Определение ОФЗ по тикеру `SU`.

---

## Итого файлов

| Файл | Действие |
|---|---|
| `app/ui/404.html` | Создать (~60 строк) |
| `app/main.py` | Изменить: path + 404 handler + POST /waitlist |
| `app/services/storage_service.py` | Изменить: таблица waitlist + методы |
| `app/ui/landing.html` | Изменить: +2 секции, waitlist form, live demo JS |

## Проверка

1. `GET /nonexistent` → HTML 404 «Страница не найдена»
2. `GET /auth/nonexistent` → JSON 404
3. `POST /waitlist {"email":"test@test.com"}` → 201
4. Повторный POST → 200 «Вы уже в списке»
5. Pro-карточка: ввести email → зелёное сообщение
6. Демо-таблица: реальные данные MOEX (или fallback статика)
7. Сравнительная таблица между Demo и «Как это работает»
8. Счётчики анимируются при скролле
9. `MVP_JWT_SECRET=<YOUR_JWT_SECRET> .venv/bin/python -m pytest` — без регрессий
10. Responsive: 768px, 640px, 420px
