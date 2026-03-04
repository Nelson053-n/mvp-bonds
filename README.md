# MVP LLM Portfolio Backend

MVP backend для портфеля с источником данных MOEX.

## Что делает LLM

- определяет тип инструмента (`stock` / `bond`)
- валидирует пользовательский ввод
- формирует короткий комментарий (1 строка)

LLM не делает расчёты.

## Что делает backend

- получает рыночные данные из MOEX API
- рассчитывает текущую стоимость, прибыль и долю
- собирает строку таблицы
- передаёт в LLM только итоговые метрики для комментария
- хранит добавленные бумаги в SQLite (`data/portfolio.db`)
- подтягивает кредитный рейтинг облигации со страницы smart-lab по тикеру (например `ruAAA (03.03.2026)`), с fallback на MOEX

## Запуск

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

> Для текущего окружения Python 3.14 используются совместимые версии
> из `requirements.txt` (включая `pydantic` beta).

## Переменные окружения

Префикс переменных: `MVP_`

- `MVP_LLM_MODE=stub|openai` (по умолчанию `stub`)
- `MVP_SQLITE_DB_PATH=data/portfolio.db`
- `MVP_OPENAI_API_KEY=...` (нужен только при `openai`)
- `MVP_OPENAI_BASE_URL=https://api.openai.com/v1`
- `MVP_OPENAI_MODEL=gpt-4o-mini`

## Эндпоинты

### Дашборд

- `GET /` — одностраничный дашборд (форма добавления + таблица портфеля)
- `GET /api-info` — служебная информация по API

### `POST /portfolio/validate`

```json
{
  "user_input": {
    "ticker": "SBER",
    "quantity": 10,
    "purchase_price": 250
  }
}
```

Ответ:

```json
{
  "instrument_type": "stock",
  "validated": true,
  "warnings": []
}
```

### `POST /portfolio/instruments`

```json
{
  "ticker": "SU26238RMFS4",
  "quantity": 100,
  "purchase_price": 920
}
```

Ответ (формат строки таблицы):

```json
{
  "name": "...",
  "type": "bond",
  "current_price": 97.1,
  "purchase_price": 920,
  "quantity": 100,
  "current_value": 97100,
  "profit": 5100,
  "ai_comment": "Доходность выше средней, бумага торгуется ниже номинала."
}
```

### `GET /portfolio/table`

Возвращает таблицу портфеля с обязательными полями и полями по типу инструмента.

### `DELETE /portfolio/instruments/{item_id}`

Удаляет конкретную строку из портфеля.

### `DELETE /portfolio/instruments/cleanup/not-found`

Удаляет бумаги, которые не находятся на бирже (нет рыночных данных MOEX).

## Ограничения MVP

- без рекомендаций купить/продать
- без прогнозов, рейтингов, multi-agent, графиков
- хранение портфеля в локальном SQLite (без авторизации и многопользовательского режима)
