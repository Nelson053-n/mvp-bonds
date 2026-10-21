"""Загрузчики MOEX ISS для аналитических страниц ОФЗ.

Возвращают чистые dict/list. Все запросы идут через `moex_service._fetch`
(общий httpx-клиент и ретраи 502/500/таймаутов) — отдельных клиентов здесь
нет (урок 9ac8f44: каждый AsyncClient держит ~703 КБ SSL-контекста навсегда).
Сбой MOEX после ретраев поднимается как `DataFetchError`, пустой день — это
пустой список, а не ошибка.
"""

import logging
import re
from typing import Any

from app.config import settings
from app.exceptions import ValidationError
from app.services.moex_service import moex_service, valid_secid

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFZ_NUMBER_RE = re.compile(r"^SU(\d{5})RMFS\d$")

# MOEX отдаёт историю страницами по 100 строк; `limit=500` игнорируется.
_HISTORY_PAGE = 100
# Предохранитель от бесконечного цикла, если MOEX перестанет двигать курсор:
# 100 страниц = 10 000 торговых дней, ОФЗ столько не торгуются.
_HISTORY_MAX_PAGES = 100

# Порядок колонок = ключи строки таблицы ofz_daily (без secid в history по бумаге).
_HISTORY_COLUMNS = (
    "SECID,TRADEDATE,CLOSE,YIELDCLOSE,DURATION,NUMTRADES,VALUE,"
    "ZSPREAD,FACEVALUE,IRICPICLOSE,BEICLOSE"
)
_HISTORY_COLUMNS_ONE = _HISTORY_COLUMNS.split(",", 1)[1]

_BOARD_BASE = "/history/engines/stock/markets/bonds/boards/TQOB/securities"

_DAILY_FLOAT_KEYS = (
    "close", "yieldclose", "duration", "value", "zspread",
    "facevalue", "iricpiclose", "beiclose",
)


def _check_date(date_str: str) -> None:
    # Дата подставляется в URL: проверка формата, как valid_secid для тикера.
    if not _DATE_RE.fullmatch(date_str or ""):
        raise ValidationError(f"Неверная дата: {date_str!r}")


def _check_secid(secid: str) -> None:
    if not valid_secid(secid or ""):
        raise ValidationError(f"Неверный SECID: {secid!r}")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def _rows(block: Any) -> list[dict[str, Any]]:
    """Блок ISS {'columns': [...], 'data': [[...]]} → список dict."""
    if not isinstance(block, dict):
        return []
    cols = block.get("columns") or []
    return [dict(zip(cols, r)) for r in (block.get("data") or [])]


def _daily_row(raw: dict[str, Any], secid: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "secid": secid if secid is not None else raw.get("SECID"),
        "tradedate": raw.get("TRADEDATE"),
        "numtrades": _int(raw.get("NUMTRADES")),
    }
    for key in _DAILY_FLOAT_KEYS:
        row[key] = _num(raw.get(key.upper()))
    return row


async def fetch_board_day(date_str: str) -> list[dict]:
    """Вся доска TQOB за день одним запросом. Выходной/нет итогов → []."""
    _check_date(date_str)
    url = (
        f"{settings.moex_base_url}{_BOARD_BASE}.json"
        f"?iss.meta=off&date={date_str}&history.columns={_HISTORY_COLUMNS}"
    )
    data = await moex_service._fetch(url)
    return [_daily_row(r) for r in _rows(data.get("history")) if r.get("SECID")]


async def fetch_secid_history(secid: str, date_from: str) -> list[dict]:
    """История одной бумаги с даты `date_from`, все страницы по 100 строк."""
    _check_secid(secid)
    _check_date(date_from)
    out: list[dict] = []
    start = 0
    for _ in range(_HISTORY_MAX_PAGES):
        url = (
            f"{settings.moex_base_url}{_BOARD_BASE}/{secid}.json"
            f"?iss.meta=off&from={date_from}&start={start}"
            f"&history.columns={_HISTORY_COLUMNS_ONE}"
        )
        data = await moex_service._fetch(url)
        page = _rows(data.get("history"))
        out.extend(_daily_row(r, secid) for r in page)
        cursor = _rows(data.get("history.cursor"))
        total = _int(cursor[0].get("TOTAL")) if cursor else None
        start += _HISTORY_PAGE
        # Конец: страница короче 100 строк либо курсор говорит, что дальше нет.
        if len(page) < _HISTORY_PAGE or (total is not None and start >= total):
            break
    else:
        logger.warning("ofz history %s: достигнут лимит страниц %d", secid, _HISTORY_MAX_PAGES)
    return out


async def fetch_zcyc_day(date_str: str) -> list[tuple[float, float]]:
    """Узлы G-кривой (period_years, yield_pct) за день. Выходной → [].

    Берётся `yearyields` из zcyc.json?date=..., а не history/zcyc: тот отдаёт
    интрадей-тики (2,5 МБ за полтора месяца).
    """
    _check_date(date_str)
    url = (
        f"{settings.moex_base_url}/engines/stock/zcyc.json"
        f"?iss.meta=off&date={date_str}&iss.only=yearyields"
    )
    data = await moex_service._fetch(url)
    nodes: list[tuple[float, float]] = []
    for r in _rows(data.get("yearyields")):
        period, value = _num(r.get("period")), _num(r.get("value"))
        if period is not None and value is not None:
            nodes.append((period, value))
    return sorted(nodes)


# --- справочник TQOB -------------------------------------------------------

_SECURITIES_KEYS = (
    "secid", "shortname", "secname", "faceunit", "status", "matdate",
    "couponperiod", "bondtype", "facevalue", "issuesize", "couponpercent",
    "couponvalue", "nextcoupon", "regnumber", "isin",
)
_SECURITIES_FLOATS = {"facevalue", "issuesize", "couponpercent", "couponvalue"}


def classify_kind(bondtype: str | None, faceunit: str | None) -> str:
    """'fixed' (ПД) | 'float' (ПК) | 'linker' (ИН) | 'amort' (АД) | 'fx' | 'other'.

    Валюта проверяется первой: у юаневых ОФЗ BONDTYPE — «Валютная облигация»,
    а не тип купона, и отличает их именно FACEUNIT != SUR. Тип ищется по
    корню слова, а не по точной строке: MOEX уже меняла формулировки
    (флоатеры, см. test_floater_bondtype).
    """
    if faceunit and faceunit.upper() not in ("SUR", "RUB"):
        return "fx"
    bt = (bondtype or "").lower()
    if "плавающ" in bt:
        return "float"
    if "линкер" in bt or "индексир" in bt:
        return "linker"
    if "амортиз" in bt:
        return "amort"
    if "фиксирован" in bt:
        return "fixed"
    return "other"


async def fetch_tqob_securities() -> list[dict]:
    """Справочник доски TQOB одним запросом, ключи строчными + `kind`."""
    url = (
        f"{settings.moex_base_url}/engines/stock/markets/bonds/boards/TQOB/"
        f"securities.json?iss.meta=off&iss.only=securities"
        f"&securities.columns={','.join(k.upper() for k in _SECURITIES_KEYS)}"
    )
    data = await moex_service._fetch(url)
    out: list[dict] = []
    for r in _rows(data.get("securities")):
        if not r.get("SECID"):
            continue
        row = {k: r.get(k.upper()) for k in _SECURITIES_KEYS}
        for k in _SECURITIES_FLOATS:
            row[k] = _num(row[k])
        row["couponperiod"] = _int(row["couponperiod"])
        row["kind"] = classify_kind(row["bondtype"], row["faceunit"])
        out.append(row)
    return out


def ofz_number(secid: str) -> str | None:
    """'SU26238RMFS4' → '26238'; прочее (RU000A…, мусор) → None."""
    m = _OFZ_NUMBER_RE.fullmatch(secid or "")
    return m.group(1) if m else None


def secid_by_number(num: str, securities: list[dict]) -> str | None:
    for s in securities:
        if ofz_number(s.get("secid")) == num:
            return s["secid"]
    return None


# --- description / bondization --------------------------------------------

_DESC_TEXT = {
    "secid": "SECID", "name": "NAME", "shortname": "SHORTNAME",
    "regnumber": "REGNUMBER", "isin": "ISIN", "faceunit": "FACEUNIT",
    "issuedate": "ISSUEDATE", "matdate": "MATDATE", "coupondate": "COUPONDATE",
    "startdatemoex": "STARTDATEMOEX",
    "bond_type": "BOND_TYPE", "bond_subtype": "BOND_SUBTYPE",
    "facevalue_type": "FACEVALUE_TYPE", "coupon_details": "COUPON_DETAILS",
    "coupon_benchmark": "COUPON_BENCHMARK",
}
_DESC_FLOAT = {
    "initialfacevalue": "INITIALFACEVALUE", "facevalue": "FACEVALUE",
    "couponpercent": "COUPONPERCENT", "couponvalue": "COUPONVALUE",
    "coupon_benchmark_spread": "COUPON_BENCHMARK_SPREAD",
}
_DESC_INT = {
    "issuesize": "ISSUESIZE", "couponfrequency": "COUPONFREQUENCY",
    "listlevel": "LISTLEVEL",
}


async def fetch_description(secid: str) -> dict:
    """Разбор description.json в плоский dict (ключи строчные, нет → None).

    `amortbond` — bool: MOEX присылает AMORTBOND только у амортизируемых,
    у остальных поля нет вовсе (→ None, а не False: «не знаем» ≠ «нет»).
    """
    _check_secid(secid)
    url = f"{settings.moex_base_url}/securities/{secid}/description.json?iss.meta=off"
    data = await moex_service._fetch(url)
    raw = {r.get("name"): r.get("value") for r in _rows(data.get("description"))}
    out: dict[str, Any] = {}
    for key, src in _DESC_TEXT.items():
        out[key] = raw.get(src) or None
    for key, src in _DESC_FLOAT.items():
        out[key] = _num(raw.get(src))
    for key, src in _DESC_INT.items():
        out[key] = _int(raw.get(src))
    am = raw.get("AMORTBOND")
    out["amortbond"] = None if am is None else str(am) == "1"
    return out


async def fetch_bondization(secid: str) -> dict:
    """{'coupons': [...], 'amortizations': [...]} из bondization.json.

    Будущие купоны флоатеров приходят с value=None — остаются None, не 0:
    ноль означал бы «купон известен и равен нулю».
    """
    _check_secid(secid)
    url = (
        f"{settings.moex_base_url}/securities/{secid}/bondization.json"
        "?iss.meta=off&iss.only=coupons,amortizations&limit=100"
    )
    data = await moex_service._fetch(url)
    coupons = [
        {
            "date": r.get("coupondate"),
            "recorddate": r.get("recorddate"),
            "startdate": r.get("startdate"),
            "value": _num(r.get("value")),
            "valueprc": _num(r.get("valueprc")),
            "facevalue": _num(r.get("facevalue")),
        }
        for r in _rows(data.get("coupons"))
    ]
    amortizations = [
        {
            "date": r.get("amortdate"),
            "value": _num(r.get("value")),
            "valueprc": _num(r.get("valueprc")),
            "kind": r.get("data_source"),
        }
        for r in _rows(data.get("amortizations"))
    ]
    for name, got in (("coupons", coupons), ("amortizations", amortizations)):
        total = _rows(data.get(f"{name}.cursor"))
        if total and (_int(total[0].get("TOTAL")) or 0) > len(got):
            logger.warning("bondization %s %s: получено %d из %s", secid, name, len(got), total[0].get("TOTAL"))
    return {"coupons": coupons, "amortizations": amortizations}
