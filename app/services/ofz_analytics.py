"""Чистая математика для аналитических страниц ОФЗ (/ofz/{номер}).

Модуль без I/O: не ходит в сеть и в SQLite, ничего не импортирует из
storage/moex. Входные данные — списки/словари, которые собирают T1/T2.
Python 3.10 совместимо.

Соглашения:
  * доходности — в процентах (16.23), дюрация MOEX — в ДНЯХ (Маколея);
  * кривая — узлы (период_в_годах, доходность_%) из zcyc.yearyields;
  * даты — строки ISO 'YYYY-MM-DD' (сравнение строк = сравнение дат).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence

Nodes = Sequence[tuple]

# Порядок групп в результате select_ofz фиксирован, чтобы список страниц
# не прыгал между прогонами.
_KIND_ORDER = ("fixed", "linker", "amort", "float")
_MANDATORY_KINDS = ("fixed", "linker", "amort")


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------

def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Перцентиль q (0..100) с линейной интерполяцией между соседними
    порядковыми статистиками (как numpy по умолчанию): на коротких рядах
    (20-250 точек) ступенчатый перцентиль заметно дёргается от одной точки."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    q = min(max(q, 0.0), 100.0)
    pos = (len(xs) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def median(values: Iterable[float]) -> Optional[float]:
    """Медиана; для пустого ряда None. Нужна вместо среднего: выбросы
    (аномальный день в спреде/обороте) среднее сдвигают, медиану — нет."""
    return percentile(values, 50)


# --------------------------------------------------------------------------
# Кривая и спред
# --------------------------------------------------------------------------

def interp_curve(nodes: Nodes, d_years: float) -> Optional[float]:
    """Доходность кривой на дюрации d_years: кусочно-линейно между узлами.

    Без экстраполяции: за пределами [min, max] узлов линейное продолжение
    последнего отрезка выдало бы правдоподобное, но ничем не подтверждённое
    число, и «спред» стал бы артефактом. Узлы могут прийти неотсортированными
    (порядок в ответе MOEX не гарантируем)."""
    if d_years is None or not nodes:
        return None
    try:
        d = float(d_years)
    except (TypeError, ValueError):
        return None
    if math.isnan(d):
        return None
    pts = sorted((float(p), float(v)) for p, v in nodes if p is not None and v is not None)
    if not pts:
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= d <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (d - x0) / (x1 - x0)
    # d вне [min, max] отрезков не нашлось — это и есть отказ от экстраполяции;
    # единственный узел годится, только если d точно на нём
    return pts[0][1] if len(pts) == 1 and d == pts[0][0] else None


def spread_bp(yield_pct: Optional[float], duration_days: Optional[float],
              nodes: Nodes) -> Optional[float]:
    """Спред к G-кривой в б.п.: (доходность бумаги − кривая на её дюрации)·100.

    Знак: «+» — бумага доходнее кривой на той же дюрации, «−» — ниже.
    Дюрация MOEX в днях, кривая индексирована годами, поэтому /365 (так же
    считает сам MOEX). Нет доходности (ПК отдают 0/None) или дюрации — None."""
    if not yield_pct or not duration_days:
        return None
    c = interp_curve(nodes, float(duration_days) / 365.0)
    if c is None:
        return None
    return (float(yield_pct) - c) * 100.0


def spread_series(hist_rows: Iterable[dict],
                  curve_by_date: dict) -> list:
    """Ряд спреда [(дата, б.п.)] по возрастанию даты.

    День пропускается, если нет кривой на эту дату, доходности или дюрации:
    подставлять соседнюю кривую нельзя — спред смешал бы движение бумаги и
    движение кривой за разные дни."""
    out = []
    for r in sorted(hist_rows, key=lambda x: x.get("tradedate") or ""):
        dt = r.get("tradedate")
        nodes = curve_by_date.get(dt)
        if not dt or not nodes:
            continue
        s = spread_bp(r.get("yieldclose"), r.get("duration"), nodes)
        if s is not None:
            out.append((dt, s))
    return out


# --------------------------------------------------------------------------
# Дюрация, выпуклость, сценарии
# --------------------------------------------------------------------------

def freq_from_period(couponperiod_days: Optional[float]) -> int:
    """Выплат в год по COUPONPERIOD в днях: 182→2, 91/92→4, 365→1.
    Остальное — round(365/period), но не меньше 1: купонные периоды ОФЗ не
    кратны ровно 365, поэтому точное сравнение не годится."""
    if not couponperiod_days or couponperiod_days <= 0:
        return 1
    return max(1, int(round(365.0 / float(couponperiod_days))))


def modified_duration(duration_days: Optional[float], yield_pct: Optional[float],
                      freq: int) -> Optional[float]:
    """Модифицированная дюрация в годах: (D/365) / (1 + y/f).

    Это производная цены по доходности при ПЕРИОДИЧЕСКОМ начислении f раз в
    год. Если доходность эффективная годовая (как YIELDCLOSE у MOEX), согласованная
    формула — f=1 (см. convexity_from_cashflows); f из купонного периода даёт
    MD ниже примерно на 7% при y≈16%."""
    if duration_days is None or yield_pct is None or not freq:
        return None
    return (float(duration_days) / 365.0) / (1.0 + float(yield_pct) / 100.0 / freq)


def _pv_terms(cashflows: Iterable[tuple], y_pct: float, freq: int):
    """[(t, pv)] для потоков (t в годах, сумма) при начислении freq раз в год.
    Потоки с t<=0 (уже прошедшие) отбрасываются: они не входят в цену."""
    if not freq or freq < 1:
        raise ValueError("freq must be >= 1")
    r = 1.0 + float(y_pct) / 100.0 / freq
    if r <= 0:
        raise ValueError("yield too low")
    return [(float(t), float(cf) / r ** (freq * float(t)))
            for t, cf in cashflows if t is not None and float(t) > 0 and cf]


def price_from_cashflows(cashflows: Iterable[tuple], y_pct: float,
                         freq: int = 1) -> float:
    """Грязная цена = сумма PV потоков, в тех же единицах, что и потоки.
    Контроль: при доходности MOEX должна совпасть с ценой+НКД."""
    return sum(pv for _, pv in _pv_terms(cashflows, y_pct, freq))


def macaulay_from_cashflows(cashflows: Iterable[tuple], y_pct: float,
                            freq: int = 1) -> Optional[float]:
    """Дюрация Маколея в ГОДАХ: Σ t·PV / Σ PV — средневзвешенный срок
    потоков, веса — их приведённая стоимость. Нужна как независимый контроль
    поля DURATION MOEX (оно в днях). freq=1 — эффективная годовая доходность,
    в какой MOEX публикует YIELDCLOSE."""
    terms = _pv_terms(cashflows, y_pct, freq)
    total = sum(pv for _, pv in terms)
    if total <= 0:
        return None
    return sum(t * pv for t, pv in terms) / total


def convexity_from_cashflows(cashflows: Iterable[tuple], y_pct: float,
                             freq: int = 1) -> Optional[float]:
    """Выпуклость (в годах²) — вторая производная цены по доходности,
    делённая на цену, при дисконтировании PV = CF/(1+y/f)^(f·t):

        C = Σ t·(t + 1/f)·PV / ( P · (1 + y/f)² ).

    При freq=1 (эффективная годовая доходность, как у MOEX) это обычное
    Σ t(t+1)·PV / (P(1+y)²); именно оно согласовано с MD = D/(1+y). Тогда
    ΔP/P ≈ −MD·Δy + ½·C·Δy² верно до второго порядка. Смешивать MD с f=2 и
    выпуклость с f=1 нельзя — сценарии получат систематическую ошибку."""
    terms = _pv_terms(cashflows, y_pct, freq)
    total = sum(pv for _, pv in terms)
    if total <= 0:
        return None
    r = 1.0 + float(y_pct) / 100.0 / freq
    return sum(t * (t + 1.0 / freq) * pv for t, pv in terms) / (total * r * r)


def price_scenarios(price_pct: float, face_value_rub: float, md: Optional[float],
                    conv: Optional[float],
                    shifts_bp: Sequence[int] = (-300, -200, -100, 100, 200, 300),
                    aci_pct: float = 0.0) -> list:
    """Модельное изменение цены при параллельном сдвиге доходности.

    Относительное изменение ГРЯЗНОЙ цены: dp = −MD·Δy + ½·C·Δy² (Δy в долях,
    поэтому shift_bp/10000). MD и C определены для грязной цены (с НКД),
    а НКД от сдвига доходности не зависит — значит, в котировку (чистую цену)
    уходит абсолютное изменение dirty·dp, а не price·dp. aci_pct — НКД в % от
    номинала; при 0 строки совпадают с простой формулой от котировки.

    Строка: shift_bp, dp_pct (отн. изменение грязной цены, %), price_pct_new,
    price_rub_new, dp_rub (изменение стоимости одной бумаги, ₽, на номинал
    face_value_rub). conv=None → только линейная часть, linear_only=True.
    md=None → пустой список."""
    if md is None or price_pct is None:
        return []
    dirty = float(price_pct) + float(aci_pct or 0.0)
    rows = []
    for s in shifts_bp:
        dy = s / 10000.0
        rel = -md * dy
        if conv is not None:
            rel += 0.5 * conv * dy * dy
        new_pct = float(price_pct) + dirty * rel
        rows.append({
            "shift_bp": s,
            "dp_pct": rel * 100.0,
            "price_pct_new": new_pct,
            "price_rub_new": float(face_value_rub) * new_pct / 100.0,
            "dp_rub": float(face_value_rub) * (new_pct - float(price_pct)) / 100.0,
            "linear_only": conv is None,
        })
    return rows


# --------------------------------------------------------------------------
# Ряды
# --------------------------------------------------------------------------

def range_stats(series: Sequence[tuple], window_days: int = 365) -> Optional[dict]:
    """min/max/last за окно window_days, заканчивающееся на последней дате
    ряда (а не на «сегодня»: на выходных и при задержке MOEX окно не должно
    съезжать). pos = (last−min)/(max−min) ∈ [0,1]; если max==min — 0.5.
    При равных экстремумах min_date/max_date — первая по времени дата."""
    pts = sorted((d, v) for d, v in series if d and v is not None)
    if not pts:
        return None
    last_d = date.fromisoformat(pts[-1][0])
    start = (last_d - timedelta(days=window_days)).isoformat()
    win = [(d, v) for d, v in pts if d >= start]
    lo = min(win, key=lambda p: p[1])
    hi = max(win, key=lambda p: p[1])
    last = win[-1][1]
    pos = 0.5 if hi[1] == lo[1] else (last - lo[1]) / (hi[1] - lo[1])
    return {"min": lo[1], "max": hi[1], "pos": pos,
            "min_date": lo[0], "max_date": hi[0], "last": last}


def weekly_downsample(series: Sequence[tuple]) -> list:
    """Одна точка на ISO-неделю — последняя торговая в неделе (для цены и
    доходности «значение на конец недели» честнее среднего). Для горизонта
    >1 года: ~150 точек за 3 года вместо ~750, график остаётся лёгким."""
    last_in_week: dict = {}
    for d, v in sorted(series, key=lambda p: p[0]):
        iso = date.fromisoformat(d).isocalendar()
        last_in_week[(iso[0], iso[1])] = (d, v)
    return sorted(last_in_week.values(), key=lambda p: p[0])


# --------------------------------------------------------------------------
# Отбор выпусков и ликвидность
# --------------------------------------------------------------------------

def _window_dates(window_rows: Sequence[dict], window: int) -> list:
    """Последние `window` торговых дат: дата торговая, если по ней есть хоть
    одна строка доски. Выходные MOEX отдаёт пустыми, так что отдельный
    календарь не нужен."""
    dates = sorted({r["tradedate"] for r in window_rows if r.get("tradedate")})
    return dates[-window:]


def _per_secid_series(window_rows: Sequence[dict], dates: list, field: str) -> dict:
    """{secid: [значение по каждой дате окна]}; нет строки/None → 0.
    Нулём заполняем осознанно: день без сделок — это ноль сделок, и он
    должен тянуть медиану вниз."""
    idx = {d: i for i, d in enumerate(dates)}
    out: dict = {}
    for r in window_rows:
        i = idx.get(r.get("tradedate"))
        if i is None:
            continue
        arr = out.setdefault(r["secid"], [0.0] * len(dates))
        arr[i] = float(r.get(field) or 0.0)
    return out


def select_ofz(securities: Sequence[dict], window_rows: Sequence[dict], n: int = 50,
               min_days: int = 15, window: int = 20,
               min_median_trades: int = 20) -> list:
    """Список secid для детальных страниц (правило §5 спеки, без списка в коде).

    Кандидат: kind != 'fx', faceunit == 'SUR', status == 'A', сделки были
    минимум в min_days из последних `window` торговых дней, медиана
    numtrades за окно ≥ min_median_trades. Из кандидатов берутся ВСЕ
    fixed/linker/amort (разнообразие типов важнее оборота: чистый топ по
    обороту выкидывает все амортизируемые и почти все линкеры), остаток до n
    добивается float по убыванию медианы оборота. Если обязательных больше n,
    урезаются по обороту. Порядок результата: группа (fixed, linker, amort,
    float), внутри — по медиане оборота по убыванию, при равенстве по secid."""
    dates = _window_dates(window_rows, window)
    if not dates:
        return []
    trades = _per_secid_series(window_rows, dates, "numtrades")
    values = _per_secid_series(window_rows, dates, "value")

    cands = []  # (kind, med_value, secid)
    for s in securities:
        kind = s.get("kind")
        if kind == "fx" or s.get("faceunit") != "SUR" or s.get("status") != "A":
            continue
        t = trades.get(s["secid"])
        if not t or sum(1 for x in t if x > 0) < min_days:
            continue
        if median(t) < min_median_trades:
            continue
        cands.append((kind, median(values.get(s["secid"], [0.0])), s["secid"]))

    def by_turnover(c):
        return (-c[1], c[2])

    mandatory = sorted((c for c in cands if c[0] in _MANDATORY_KINDS), key=by_turnover)
    floats = sorted((c for c in cands if c[0] not in _MANDATORY_KINDS), key=by_turnover)
    if len(mandatory) > n:
        mandatory = mandatory[:n]
    chosen = mandatory + floats[:max(0, n - len(mandatory))]
    chosen.sort(key=lambda c: (_KIND_ORDER.index(c[0]) if c[0] in _KIND_ORDER
                               else len(_KIND_ORDER),) + by_turnover(c))
    return [c[2] for c in chosen]


def liquidity_rank(secid: str, window_rows: Sequence[dict], window: int = 20,
                   universe: Optional[Iterable[str]] = None) -> tuple:
    """(место, всего, медиана оборота руб/день) по медиане value за последние
    `window` торговых дат; 1 — самая ликвидная. window_rows не содержат
    валюты номинала, поэтому рублёвое множество задаёт вызывающий через
    `universe` (иначе — все secid из строк). Бумага без строк получает
    медиану 0 и последнее место."""
    dates = _window_dates(window_rows, window)
    values = _per_secid_series(window_rows, dates, "value")
    if universe is not None:
        allowed = set(universe)
        values = {k: v for k, v in values.items() if k in allowed}
    meds = {k: median(v) for k, v in values.items()}
    meds.setdefault(secid, 0.0)
    mine = meds[secid]
    place = 1 + sum(1 for k, m in meds.items() if k != secid and
                    (m > mine or (m == mine and k < secid)))
    return place, len(meds), mine


def neighbors(secid: str, board_rows: Sequence[dict], k: int = 4,
              kind_filter=None) -> list:
    """Ближайшие k выпусков по |дюрация − дюрация secid| среди строк
    ПОСЛЕДНЕГО дня; сам выпуск исключён; берутся только строки с дюрацией и
    доходностью (ПК с YIELD=0 в сравнении по доходности бессмысленны).
    kind_filter — значение или набор значений ключа 'kind' строки (строки без
    kind при фильтре отбрасываются). Результат по возрастанию расстояния,
    при равенстве — по secid; в каждый dict добавлено 'distance_days'.
    Если у самого secid нет дюрации в последний день — []."""
    rows = [r for r in board_rows if r.get("tradedate")]
    if not rows:
        return []
    last = max(r["tradedate"] for r in rows)
    day = [r for r in rows if r["tradedate"] == last]
    me = next((r for r in day if r["secid"] == secid), None)
    if not me or not me.get("duration"):
        return []
    if kind_filter is not None and not isinstance(kind_filter, (set, frozenset, list, tuple)):
        kind_filter = (kind_filter,)
    out = []
    for r in day:
        if r["secid"] == secid or not r.get("duration") or not r.get("yieldclose"):
            continue
        if kind_filter is not None and r.get("kind") not in kind_filter:
            continue
        out.append(dict(r, distance_days=abs(r["duration"] - me["duration"])))
    out.sort(key=lambda r: (r["distance_days"], r["secid"]))
    return out[:k]
