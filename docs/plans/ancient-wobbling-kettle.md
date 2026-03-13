# Bond AI v3 — Фаза 1: Критичные улучшения

## Контекст

Bond AI v3 — крупное обновление лендинга и юридической базы. Фаза 1 включает 5 задач с наибольшим влиянием на доверие и конверсию: юридические страницы, FAQ, объяснение AI-логики, дисклеймер и техническая база (favicon, Яндекс.Метрика).

Все изменения — в рамках существующего стека: vanilla HTML/CSS/JS, FastAPI, без фреймворков.

## Порядок реализации

1. Favicon (32px + apple-touch-icon 180px)
2. `/privacy` + `/terms` (новые HTML-страницы + маршруты)
3. Секция «Как работает AI» (между Features и Demo)
4. FAQ-аккордеон (между Pricing и CTA)
5. Дисклеймер-плашка (между CTA и Footer)
6. Яндекс.Метрика + goals (последним, чтобы не мешать разработке)

---

## Задача 1: Favicon

**Файлы:** `app/ui/favicon-32.png`, `app/ui/apple-touch-icon.png` (новые)

Сгенерировать из существующего `icon-192.png` через Pillow/ImageMagick (resize 32x32 и 180x180).

**`app/main.py`** — добавить 2 маршрута (по аналогии с `/icon-192.png`):
- `GET /favicon-32.png`
- `GET /apple-touch-icon.png`

**`app/ui/landing.html`** `<head>` — заменить inline SVG favicon на:
```html
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
```

**`app/ui/dashboard.html`** `<head>` — аналогичная замена.

---

## Задача 2: /privacy + /terms

### Новые файлы

**`app/ui/privacy.html`** — Политика конфиденциальности (152-ФЗ):
- Тёмная тема (тот же дизайн что landing), Inter шрифт
- Упрощённый topbar: лого + кнопка «На главную»
- 8 разделов: оператор, собираемые данные, цели, хранение, третьи лица (MOEX/Anthropic/Telegram), права пользователя, cookies, контакты
- Footer с копирайтом

**`app/ui/terms.html`** — Пользовательское соглашение:
- Тот же шаблон
- 8 разделов: общие положения, инвестиционная оговорка, AI-рекомендации, ограничение ответственности, условия бесплатного тарифа, ИС, персональные данные (ссылка на /privacy), разрешение споров

### CSS-стили для legal-страниц (встроены в каждый файл)
```css
.legal-page { padding: 100px 24px 80px; min-height: 100vh; }
.legal-container { max-width: 720px; margin: 0 auto; }
.legal-title { font-size: clamp(28px,4vw,40px); font-weight: 800; color: #fff; }
.legal-section h2 { font-size: 18px; font-weight: 700; color: #fff; }
.legal-section p, .legal-section li { font-size: 15px; color: var(--slate-400); line-height: 1.8; }
```

### Изменения в существующих файлах

**`app/main.py`:**
- Добавить path-переменные: `privacy_path`, `terms_path`
- Добавить маршруты: `GET /privacy`, `GET /terms`
- Обновить `sitemap.xml`: добавить `/privacy` и `/terms` (priority 0.3, monthly)

**`app/ui/landing.html`** — обновить footer (строка 1388):
```html
<footer class="footer">
  <p>
    &copy; 2025 Bond AI
    <span class="footer-sep">&middot;</span>
    <a href="/privacy" class="footer-link">Конфиденциальность</a>
    <span class="footer-sep">&middot;</span>
    <a href="/terms" class="footer-link">Условия использования</a>
    <span class="footer-sep">&middot;</span>
    Данные предоставлены Московской биржей
  </p>
</footer>
```

---

## Задача 3: Секция «Как работает AI»

**Расположение:** между Features (конец ~строка 1057) и Demo (начало ~строка 1059).

**HTML-структура:** 4 шага в горизонтальной сетке с соединительной линией:
1. **Данные** (синяя иконка wallet/db) — «Загружаем цены, купоны, YTM и рейтинги 1200+ облигаций с MOEX»
2. **Фильтрация** (индиго иконка filter) — «Отсеиваем неликвидные бумаги, фильтруем по рейтингу и дюрации»
3. **AI-оптимизация** (фиолетовая иконка brain) — «Claude AI составляет диверсифицированный портфель с учётом секторов и сроков»
4. **Обоснование** (зелёная иконка document) — «Для каждой позиции AI объясняет выбор: рейтинг, доходность, денежный поток»

**CSS:**
- `.ai-steps-grid` — grid 4 колонки, с `::before` gradient-линией между шагами
- `.ai-step-icon` — 80x80 rounded box с цветным фоном и тенью
- `.ai-step-arrow` — стрелка → между шагами (скрывается на мобильных)

**Responsive:**
- 768px: grid 2x2, скрыть линию и стрелки
- 640px: grid 1 колонка

---

## Задача 4: FAQ-аккордеон

**Расположение:** между Pricing (конец ~строка 1255) и CTA (начало ~строка 1257).

**8 вопросов:**
1. Bond AI — это бесплатно?
2. Нужен ли брокерский счёт?
3. Откуда берутся данные о ценах?
4. Как работает AI-подбор портфеля?
5. Является ли Bond AI инвестиционной рекомендацией?
6. Как настроить уведомления в Telegram?
7. Могу ли я поделиться портфелем?
8. Мои данные в безопасности?

**HTML:** `.faq-list` > `.faq-item` > `.faq-q` (вопрос + chevron SVG) + `.faq-a` (ответ)

**CSS:**
- `.faq-list` — max-width 760px, border-radius, 1px gap между items
- `.faq-a` — `max-height: 0; overflow: hidden; transition: max-height .35s`
- `.faq-item.open .faq-a` — `max-height: 300px`
- `.faq-item.open .faq-chevron` — `rotate(180deg)`, color blue

**JS:** функция `faqToggle(qEl)` — закрывает все, открывает кликнутый (toggle).

---

## Задача 5: Дисклеймер-плашка

**Расположение:** между CTA (конец ~строка 1266) и wizard-overlay (~строка 1268).

**HTML:** `.disclaimer-banner` > `.disclaimer-inner` (flex) > `.disclaimer-icon` (amber SVG info-circle) + `.disclaimer-text`

**Текст:** «Bond AI предоставляет аналитические инструменты и информацию на основе данных Московской биржи. Сервис не является инвестиционным советником... Подробнее →» (ссылка на /terms)

**CSS:** amber-тон — `rgba(217,119,6,.06)` фон, `rgba(217,119,6,.12)` бордер. На 640px — flex-direction: column.

---

## Задача 6: Яндекс.Метрика

**`app/main.py`** — обновить CSP в `_SECURITY_HEADERS`:
- `script-src`: добавить `https://mc.yandex.ru https://mc.yandex.com`
- `img-src`: добавить `https://mc.yandex.ru https://mc.yandex.com`
- `connect-src`: добавить `https://mc.yandex.ru https://mc.yandex.com`

**`app/ui/landing.html`** — вставить счётчик перед `</body>` (placeholder ID `XXXXXXXX`).

**`app/ui/dashboard.html`** — аналогичный счётчик.

**Goals** (вызовы `ym(ID, 'reachGoal', ...)` в существующих JS-функциях):
- `wizard_start` — в `wizOpen()`
- `wizard_complete` — после успешного результата в `wizGoStep3()`
- `registration` — после успешной регистрации в `wizRegister()`
- `login` — после успешного логина в `landingAuthSubmit()`

---

## Итого файлов

| Файл | Действие |
|---|---|
| `app/ui/favicon-32.png` | Создать (32x32) |
| `app/ui/apple-touch-icon.png` | Создать (180x180) |
| `app/ui/privacy.html` | Создать (~200 строк) |
| `app/ui/terms.html` | Создать (~200 строк) |
| `app/ui/landing.html` | Изменить: +3 секции, footer, favicon, Метрика |
| `app/ui/dashboard.html` | Изменить: favicon, Метрика |
| `app/main.py` | Изменить: +4 маршрута, CSP, sitemap |

## Проверка

1. Запустить `MVP_JWT_SECRET=REDACTED .venv/bin/uvicorn app.main:app --reload --host 192.168.10.32`
2. Открыть `http://192.168.10.32:8000/` — проверить все новые секции (AI, FAQ, дисклеймер, footer)
3. Проверить responsive: 768px, 640px, 420px
4. Открыть `/privacy` и `/terms` — проверить навигацию и контент
5. Проверить `/sitemap.xml` — должны быть 3 URL
6. Проверить favicon в табе браузера
7. DevTools Network — проверить загрузку Метрики (без CSP-ошибок)
8. Запустить `.venv/bin/python -m pytest` — без регрессий
