"""Фон для аналитических страниц ОФЗ: дозагрузка истории, отбор 50 выпусков, прогрев.

Данные берутся только через ofz_history_service (общий httpx-клиент, ретраи),
хранятся в ofz_daily / ofz_curve_daily. Цикл `ofz_warm_loop` запускается
только в leader-воркере (см. main.py).
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

from app.services import ofz_history_service as hist
from app.services.ofz_analytics import select_ofz
from app.services.storage_service import storage_service

logger = logging.getLogger(__name__)

# Москва = UTC+3 без перехода на летнее время (с 2014 г.).
_MSK = timezone(timedelta(hours=3))

_SECURITIES_TTL = 24 * 3600
_SELECTION_TTL = 6 * 3600
# После сбоя MOEX справочник не перезапрашивается на каждый вызов страницы.
_SECURITIES_RETRY = 60
_WINDOW_DAYS = 20

# Защита: за один прогон не больше стольких календарных дней, остальное
# дотянется следующим.
_MAX_CATCHUP_DAYS = 40
_BACKFILL_PAUSE = 0.15
_WARM_CONCURRENCY = 2
_STARTUP_DELAY = 30
# Итоги дня в history появляются вечером, ночной прогон берёт вчерашний день.
_RUN_HOUR_MSK, _RUN_MINUTE_MSK = 3, 30
_FAILURE_ALERT_THRESHOLD = 3

_sec_cache: tuple | None = None       # (monotonic, list[dict])
_sec_fail_at: float | None = None
_sec_inflight: "asyncio.Future | None" = None
_sel_cache: tuple | None = None       # (monotonic, list[dict])
_backfill_running = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _yesterday_msk() -> date:
    # Сегодняшний день не берём: итоги торгов ещё не опубликованы.
    return (_now().astimezone(_MSK) - timedelta(days=1)).date()


def reset_caches() -> None:
    global _sec_cache, _sec_fail_at, _sel_cache
    _sec_cache = None
    _sec_fail_at = None
    _sel_cache = None


# --- выбор выпусков --------------------------------------------------------

async def _load_securities() -> list:
    """Справочник TQOB; при сбое — прежний кэш, иначе []. Не бросает."""
    global _sec_cache, _sec_fail_at
    try:
        data = await hist.fetch_tqob_securities()
    except Exception:
        _sec_fail_at = _monotonic()
        logger.exception("ofz_warm: справочник TQOB не получен")
        return _sec_cache[1] if _sec_cache else []
    _sec_cache = (_monotonic(), data)
    _sec_fail_at = None
    return data


async def _get_securities() -> list:
    global _sec_inflight
    now = _monotonic()
    if _sec_cache and now - _sec_cache[0] < _SECURITIES_TTL:
        return _sec_cache[1]
    if _sec_fail_at is not None and now - _sec_fail_at < _SECURITIES_RETRY:
        return _sec_cache[1] if _sec_cache else []
    # Single-flight: параллельные вызовы ждут один запрос к MOEX.
    if _sec_inflight is None or _sec_inflight.done():
        _sec_inflight = asyncio.ensure_future(_load_securities())
    return await asyncio.shield(_sec_inflight)


async def get_selection() -> list:
    """Выбранные выпуски (≤50) как dict справочника TQOB + 'number'.

    Пустая таблица ofz_daily или недоступный справочник без кэша → [] (пустой
    результат не кэшируется, чтобы страница заработала сразу после бэкфила).
    """
    global _sel_cache
    now = _monotonic()
    if _sel_cache and now - _sel_cache[0] < _SELECTION_TTL:
        return _sel_cache[1]
    window = storage_service.get_ofz_window(_WINDOW_DAYS)
    if not window:
        return []
    securities = await _get_securities()
    if not securities:
        return []
    by_secid = {s["secid"]: s for s in securities}
    result = []
    for secid in select_ofz(securities, window):
        number = hist.ofz_number(secid)
        if number:
            result.append(dict(by_secid[secid], number=number))
    if result:
        _sel_cache = (_monotonic(), result)
    return result


# --- загрузка истории ------------------------------------------------------

async def _load_day(day: str) -> tuple:
    """(строк доски, узлов кривой) за день; выходной → (0, 0), кривая не запрашивается."""
    rows = await hist.fetch_board_day(day)
    if not rows:
        return 0, 0
    n_rows = storage_service.upsert_ofz_daily(rows)
    nodes = await hist.fetch_zcyc_day(day)
    n_nodes = storage_service.upsert_ofz_curve(day, nodes) if nodes else 0
    return n_rows, n_nodes


async def backfill(days: int = 365) -> dict:
    """Разовая загрузка назад от вчера; только при пустой ofz_daily.

    Строго последовательно с паузой: ~500 запросов подряд не должны давить
    общий пул клиента, на котором сидят и портфели.
    """
    global _backfill_running
    stats = {"dates_loaded": 0, "rows": 0, "curve_nodes": 0}
    if storage_service.get_ofz_last_date() is not None or _backfill_running:
        logger.info("ofz backfill: пропуск (таблица не пуста или уже идёт)")
        return stats
    _backfill_running = True
    failed = 0
    try:
        end = _yesterday_msk()
        for i in range(days):
            day = (end - timedelta(days=i)).isoformat()
            try:
                n_rows, n_nodes = await _load_day(day)
            except Exception as exc:
                failed += 1
                logger.warning("ofz backfill %s: %s", day, exc or type(exc).__name__)
                n_rows = n_nodes = 0
            if n_rows:
                stats["dates_loaded"] += 1
                stats["rows"] += n_rows
                stats["curve_nodes"] += n_nodes
            await _sleep(_BACKFILL_PAUSE)
    finally:
        _backfill_running = False
    reset_caches()
    logger.info("ofz backfill: дней %d, строк %d, узлов кривой %d, ошибок %d",
                stats["dates_loaded"], stats["rows"], stats["curve_nodes"], failed)
    return stats


async def run_daily_update() -> dict:
    """Дотянуть пропущенные торговые даты до вчера (по Москве).

    Сбой на дате прерывает прогон исключением: если пропустить её и взять
    следующие, MAX(tradedate) уйдёт вперёд и дыра больше не заполнится.
    """
    last = storage_service.get_ofz_last_date()
    if last is None:
        return await backfill()
    stats = {"dates_loaded": 0, "rows": 0, "curve_nodes": 0}
    day = date.fromisoformat(last) + timedelta(days=1)
    end = _yesterday_msk()
    steps = 0
    try:
        while day <= end and steps < _MAX_CATCHUP_DAYS:
            n_rows, n_nodes = await _load_day(day.isoformat())
            if n_rows:
                stats["dates_loaded"] += 1
                stats["rows"] += n_rows
                stats["curve_nodes"] += n_nodes
            day += timedelta(days=1)
            steps += 1
    finally:
        # Окно отбора изменилось, даже если прогон прервался на полпути.
        reset_caches()
    logger.info("ofz daily update: %s", stats)
    return stats


# --- прогрев страниц и цикл ------------------------------------------------

async def warm_pages(selection: list) -> tuple:
    """Прогрев страниц выбранных выпусков, ≤2 одновременно. (прогрето, сбоев)."""
    try:
        from app.api import ofz_pages
    except ImportError:
        logger.info("ofz_warm: модуля ofz_pages нет, прогрев пропущен")
        return 0, 0
    warm = getattr(ofz_pages, "warm", None)
    if warm is None:
        logger.info("ofz_warm: ofz_pages.warm нет, прогрев пропущен")
        return 0, 0
    sem = asyncio.Semaphore(_WARM_CONCURRENCY)
    counts = {"ok": 0, "fail": 0}

    async def one(number: str) -> None:
        async with sem:
            try:
                ok = await warm(number)
            except Exception:
                logger.exception("ofz warm %s failed", number)
                ok = False
        counts["ok" if ok else "fail"] += 1

    await asyncio.gather(*(one(s["number"]) for s in selection))
    logger.info("ofz warm: прогрето %d, сбоев %d", counts["ok"], counts["fail"])
    return counts["ok"], counts["fail"]


def _secs_to_next_run() -> float:
    now = _now().astimezone(_MSK)
    target = now.replace(hour=_RUN_HOUR_MSK, minute=_RUN_MINUTE_MSK, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _alert(streak: int, exc: Exception) -> None:
    # Ленивый импорт: app.main импортирует этот модуль в lifespan.
    from app.main import _alert_task_failure
    await _alert_task_failure("Данные ОФЗ (история и прогрев)", streak, exc)


async def ofz_warm_loop():
    """Ежедневно 03:30 МСК: дозагрузка истории → прогрев страниц. Для leader-воркера."""
    await _sleep(_STARTUP_DELAY)
    try:
        if storage_service.get_ofz_last_date() is None:
            await backfill()
    except Exception:
        logger.exception("ofz backfill failed")

    fail_streak = 0
    while True:
        await _sleep(_secs_to_next_run())
        try:
            await run_daily_update()
            await warm_pages(await get_selection())
            fail_streak = 0
        except Exception as exc:
            fail_streak += 1
            logger.exception("ofz daily update failed (streak=%d)", fail_streak)
            if fail_streak >= _FAILURE_ALERT_THRESHOLD:
                await _alert(fail_streak, exc)
