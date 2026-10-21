"""Серверные inline-SVG графики для аналитических страниц ОФЗ.

Без I/O и без JS: функции возвращают строку `<svg>`. Цвета заданы только через
CSS-переменные шелла страницы (`var(--link)`, `var(--line-3)`, `var(--muted)` ...),
поэтому светлая/тёмная тема переключается без перерисовки. Размеров в px на корне
нет: масштаб даёт viewBox + `width:100%;height:auto`.
"""
import hashlib
import math
from datetime import date
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple

W, H = 640, 260
# Область построения; снизу оставлено место под подписи оси X.
PL, PR, PT, PB = 46, 18, 16, 32
X0, X1, Y0, Y1 = PL, W - PR, PT, H - PB

# Лимит точек в path: 250 точек линии иначе не укладываются в бюджет 4 КБ на график.
MAX_POINTS = 150
MAX_POINTS_SPREAD = 100  # у спреда path строится дважды (заливка и линия)

_MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

_SVG_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 260" '
             'style="width:100%;height:auto" role="img" aria-label="{t}">')
# Размер и цвет подписей заданы один раз на <g>: повтор style у каждого <text> стоил ~35% веса графика.
_G_OPEN = '<g style="font-size:11px;font-family:inherit" fill="var(--muted)">'


def _esc(s) -> str:
    return escape(str(s), quote=True)


def _n(v: float) -> str:
    """Координата с 1 знаком; хвост '.0' отбрасываем ради размера."""
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _fmt(v: float, d: int) -> str:
    s = f"{v:.{d}f}"
    if float(s) == 0:
        s = s.lstrip("-")  # «-0,00» после округления выглядит как ошибка
    return s.replace("-", "\u2212").replace(".", ",")


def _nice_step(rng: float, target: int) -> float:
    """Тот же ряд множителей (1, 2, 2.5, 5, 10), что в niceStep из _MAP_JS."""
    raw = rng / target
    mag = 10 ** math.floor(math.log10(raw))
    for c in (1, 2, 2.5, 5, 10):
        if c * mag >= raw:
            return c * mag
    return 10 * mag


def _nice_axis(lo: float, hi: float, target: int = 4) -> Tuple[float, float, float, List[float]]:
    if hi - lo < 1e-9:
        pad = max(abs(lo) * 0.05, 0.5)
        lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo, target)
    a = math.floor(lo / step + 1e-9) * step
    b = math.ceil(hi / step - 1e-9) * step
    ticks, k = [], 0
    while a + k * step <= b + step * 1e-6 and k < 50:
        ticks.append(round(a + k * step, 10))
        k += 1
    return a, b, step, ticks


def _axis_decimals(step: float) -> int:
    d = 0
    while d < 3 and abs(step * 10 ** d - round(step * 10 ** d)) > 1e-6:
        d += 1
    return d


def _downsample(pts: list, limit: int) -> list:
    """Каждая n-я точка, последняя сохраняется всегда."""
    if len(pts) <= limit:
        return pts
    n = math.ceil(len(pts) / limit)
    out = pts[::n]
    if out[-1] is not pts[-1]:
        out.append(pts[-1])
    return out


def _stub(title: str) -> str:
    return (_SVG_OPEN.format(t=_esc(title)) + f"<title>{_esc(title)}</title>"
            f'{_G_OPEN}<text x="320" y="130" text-anchor="middle">'
            "Недостаточно данных</text></g></svg>")


def _head(title: str) -> List[str]:
    return [_SVG_OPEN.format(t=_esc(title)), f"<title>{_esc(title)}</title>", _G_OPEN]


def _y_grid(parts: List[str], ticks: List[float], ymap, dec: int) -> None:
    ys = [ymap(t) for t in ticks]
    parts.append('<path d="' + "".join(f"M{X0} {_n(y)}H{X1}" for y in ys) + '" stroke="var(--line-3)" fill="none"/>')
    for t, y in zip(ticks, ys):
        parts.append(f'<text x="{X0 - 6}" y="{_n(y + 4)}" text-anchor="end">{_fmt(t, dec)}</text>')


def _parse(series: Sequence[Tuple[str, float]]) -> List[Tuple[date, float]]:
    out = []
    for d, v in series or []:
        try:
            dt = date.fromisoformat(str(d)[:10])
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append((dt, f))
    out.sort(key=lambda p: p[0])
    return out


def _x_time(parts: List[str], d0: date, d1: date) -> "callable":
    """Ось X во времени: подписи по месяцам, не более ~6; на коротком ряду — даты dd.mm."""
    span = max((d1 - d0).days, 1)

    def xmap(d: date) -> float:
        return X0 + (d.toordinal() - d0.toordinal()) / span * (X1 - X0)

    months, y, m = [], d0.year, d0.month
    while date(y, m, 1) <= d1:
        if date(y, m, 1) >= d0:
            months.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if len(months) >= 2:
        stride = math.ceil(len(months) / 6)
        labels = [(mm, f"{_MONTHS[mm.month - 1]} {mm.year % 100:02d}") for mm in months[::stride]]
    else:
        labels = []
        for i in range(5):
            dd = date.fromordinal(d0.toordinal() + round(span * i / 4))
            labels.append((dd, f"{dd.day:02d}.{dd.month:02d}"))
    for dd, txt in labels:
        x = xmap(dd)
        parts.append(f'<path d="M{_n(x)} {Y1}v4" stroke="var(--line-3)" fill="none"/>')
        parts.append(f'<text x="{_n(x)}" y="{Y1 + 17}" text-anchor="middle">{_esc(txt)}</text>')
    return xmap


def _path(pts: List[Tuple[float, float]]) -> str:
    return "M" + "L".join(f"{_n(x)} {_n(y)}" for x, y in pts)


def line_chart(series, *, title: str, unit: str = "%", band: Optional[Tuple[float, float]] = None,
               median: Optional[float] = None, color_var: str = "--link", decimals: int = 2) -> str:
    data = _parse(series)
    if len(data) < 2:
        return _stub(title)
    vals = [v for _, v in data]
    lo, hi = min(vals), max(vals)
    for extra in ([band[0], band[1]] if band else []) + ([median] if median is not None else []):
        lo, hi = min(lo, extra), max(hi, extra)
    a, b, step, ticks = _nice_axis(lo, hi)
    dec = max(_axis_decimals(step), 0)

    def ymap(v):
        return Y1 - (v - a) / (b - a) * (Y1 - Y0)

    parts = _head(title)
    _y_grid(parts, ticks, ymap, dec)
    xmap = _x_time(parts, data[0][0], data[-1][0])
    if band:
        y_top, y_bot = ymap(max(band)), ymap(min(band))
        parts.append(f'<rect x="{X0}" y="{_n(y_top)}" width="{X1 - X0}" height="{_n(y_bot - y_top)}" '
                     f'fill="var({color_var})" fill-opacity="0.12"/>')
    if median is not None:
        ym = ymap(median)
        parts.append(f'<path d="M{X0} {_n(ym)}H{X1}" stroke="var(--faint)" stroke-width="1" '
                     'stroke-dasharray="4 3" fill="none"/>')
        parts.append(f'<text x="{X0 + 4}" y="{_n(ym - 4)}">'
                     f'медиана {_fmt(median, decimals)}{_esc(unit)}</text>')
    pts = [(xmap(d), ymap(v)) for d, v in _downsample(data, MAX_POINTS)]
    parts.append(f'<path d="{_path(pts)}" fill="none" stroke="var({color_var})" stroke-width="2" '
                 'stroke-linejoin="round"/>')
    lx, ly = xmap(data[-1][0]), ymap(data[-1][1])
    parts.append(f'<circle cx="{_n(lx)}" cy="{_n(ly)}" r="4" fill="var({color_var})"/>')
    parts.append(f'<text x="{_n(lx - 8)}" y="{_n(ly - 8)}" text-anchor="end" fill="var(--text-strong)" style="font-weight:600">{_fmt(data[-1][1], decimals)}{_esc(unit)}</text>')
    parts.append("</g></svg>")
    return "".join(parts)


def _num_pairs(items, kx: str, ky: str):
    out = []
    for it in items or []:
        try:
            x, y = (it[kx], it[ky]) if isinstance(it, dict) else (it[0], it[1])
            x, y = float(x), float(y)
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            out.append((x, y, it))
    return out


def curve_chart(nodes, points, highlight: Optional[dict] = None, *, x_max: float = 8.0) -> str:
    """nodes: [(d_years, yield)] или [{d_years, y}]; points: [{secid/label, d_years, y}]."""
    title = "Кривая бескупонной доходности и выпуски ОФЗ"
    node_xy = sorted((x, y) for x, y, _ in _num_pairs(nodes, "d_years", "y") if 0 <= x <= x_max)
    pts = [(x, y, it) for x, y, it in _num_pairs(points, "d_years", "y") if 0 <= x <= x_max]
    hl = None
    if highlight:
        h = _num_pairs([highlight], "d_years", "y")
        if h and 0 <= h[0][0] <= x_max:
            hl = h[0]
    if len(node_xy) < 2:
        return _stub(title)
    ys = [y for _, y in node_xy] + [y for _, y, _ in pts] + ([hl[1]] if hl else [])
    a, b, step, ticks = _nice_axis(min(ys), max(ys))
    dec = _axis_decimals(step)

    def ymap(v):
        return Y1 - (v - a) / (b - a) * (Y1 - Y0)

    def xmap(v):
        return X0 + v / x_max * (X1 - X0)

    parts = _head(title)
    _y_grid(parts, ticks, ymap, dec)
    xs = _nice_axis(0, x_max, 8)[3]
    for t in xs:
        if t > x_max + 1e-9:
            continue
        parts.append(f'<path d="M{_n(xmap(t))} {Y1}v4" stroke="var(--line-3)" fill="none"/>')
        parts.append(f'<text x="{_n(xmap(t))}" y="{Y1 + 17}" text-anchor="middle">{_fmt(t, 0)}</text>')
    parts.append(f'<text x="{X1}" y="{Y1 + 29}" text-anchor="end">дюрация, лет</text>')
    parts.append(f'<path d="{_path([(xmap(x), ymap(y)) for x, y in node_xy])}" fill="none" '
                 'stroke="var(--link)" stroke-width="2" stroke-linejoin="round"/>')
    parts.append('<g fill="var(--faint)">')
    for x, y, it in pts:
        label = (it.get("label") or it.get("secid") or "") if isinstance(it, dict) else ""
        tip = f"<title>{_esc(label)}</title>" if label else ""
        parts.append(f'<circle cx="{_n(xmap(x))}" cy="{_n(ymap(y))}" r="3">{tip}</circle>')
    parts.append("</g>")
    if hl:
        x, y, it = hl
        px, py = xmap(x), ymap(y)
        parts.append(f'<path d="M{_n(px)} {_n(py)}V{Y1}" stroke="var(--link)" stroke-width="1" '
                     'stroke-dasharray="4 3" fill="none"/>')
        parts.append(f'<circle cx="{_n(px)}" cy="{_n(py)}" r="6" fill="var(--link)"/>')
        label = it.get("label") or it.get("secid") or ""
        txt = f"{label} {_fmt(y, 2)}%".strip()
        anchor, tx = ("end", px - 10) if px > (X0 + X1) / 2 else ("start", px + 10)
        parts.append(f'<text x="{_n(tx)}" y="{_n(py - 10)}" text-anchor="{anchor}" '
                     f'fill="var(--text-strong)" style="font-weight:600">{_esc(txt)}</text>')
    parts.append("</g></svg>")
    return "".join(parts)


def spread_chart(series, *, median: Optional[float] = None) -> str:
    """Спред в б.п. с нулевой линией.

    Цвет по знаку ТОЛЬКО нейтральный: выше нуля заливка var(--link), ниже нуля
    var(--faint). Красный/зелёный намекали бы на оценку («плохо/хорошо»,
    «дорого/дёшево»), а спред — просто разность двух величин; страница таких
    оценок не даёт. Одна и та же фигура рисуется один раз и дважды
    показывается через <use> с clipPath выше/ниже нуля — вдвое короче, чем две
    ломаные, и пересечения нуля обрезаются точно, без расчёта точек пересечения.
    """
    title = "Спред доходности к кривой, б.п."
    data = _parse(series)
    if len(data) < 2:
        return _stub(title)
    vals = [v for _, v in data] + [0.0] + ([median] if median is not None else [])
    a, b, step, ticks = _nice_axis(min(vals), max(vals))
    dec = _axis_decimals(step)

    def ymap(v):
        return Y1 - (v - a) / (b - a) * (Y1 - Y0)

    parts = _head(title)
    _y_grid(parts, ticks, ymap, dec)
    xmap = _x_time(parts, data[0][0], data[-1][0])
    z = ymap(0.0)
    pts = [(xmap(d), ymap(v)) for d, v in _downsample(data, MAX_POINTS_SPREAD)]
    # id зависит от содержимого: детерминированно и не конфликтует между разными графиками.
    uid = hashlib.md5(_path(pts).encode()).hexdigest()[:6]
    shape = f"M{_n(pts[0][0])} {_n(z)}L" + "L".join(f"{_n(x)} {_n(y)}" for x, y in pts) + f"L{_n(pts[-1][0])} {_n(z)}"
    parts.append(f'<defs><path id="s{uid}" d="{shape}"/>'
                 f'<clipPath id="u{uid}"><rect x="{X0}" y="{Y0}" width="{X1 - X0}" height="{_n(max(z - Y0, 0))}"/></clipPath>'
                 f'<clipPath id="d{uid}"><rect x="{X0}" y="{_n(z)}" width="{X1 - X0}" height="{_n(max(Y1 - z, 0))}"/></clipPath></defs>')
    parts.append(f'<use href="#s{uid}" clip-path="url(#u{uid})" fill="var(--link)" fill-opacity="0.25"/>')
    parts.append(f'<use href="#s{uid}" clip-path="url(#d{uid})" fill="var(--faint)" fill-opacity="0.3"/>')
    parts.append(f'<path d="M{X0} {_n(z)}H{X1}" stroke="var(--muted)" stroke-width="1" fill="none"/>')
    if median is not None:
        ym = ymap(median)
        parts.append(f'<path d="M{X0} {_n(ym)}H{X1}" stroke="var(--faint)" stroke-width="1" '
                     'stroke-dasharray="4 3" fill="none"/>')
        parts.append(f'<text x="{X0 + 4}" y="{_n(ym - 4)}">'
                     f'медиана {_fmt(median, 0)} б.п.</text>')
    parts.append(f'<path d="{_path(pts)}" fill="none" stroke="var(--link)" stroke-width="1.5" '
                 'stroke-linejoin="round"/>')
    lx, ly = pts[-1]
    parts.append(f'<circle cx="{_n(lx)}" cy="{_n(ly)}" r="4" fill="var(--link)"/>')
    parts.append(f'<text x="{_n(lx - 8)}" y="{_n(ly - 8)}" text-anchor="end" fill="var(--text-strong)" style="font-weight:600">{_fmt(data[-1][1], 0)} б.п.</text>')
    parts.append("</g></svg>")
    return "".join(parts)


def bars_chart(items, *, title: str, unit: str = "") -> str:
    rows = []
    for lab, v in items or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            rows.append((str(lab), f))
    if not rows:
        return _stub(title)
    top = max(max(v for _, v in rows), 0.0)
    bot = min(min(v for _, v in rows), 0.0)
    a, b, step, ticks = _nice_axis(bot, top if top > bot else bot + 1)
    dec = _axis_decimals(step)

    def ymap(v):
        return Y1 - (v - a) / (b - a) * (Y1 - Y0)

    parts = _head(title)
    _y_grid(parts, ticks, ymap, dec)
    n = len(rows)
    slot = (X1 - X0) / n
    bw = min(slot * 0.7, 48)
    stride = math.ceil(n / 8)
    z = ymap(0.0)
    for i, (lab, v) in enumerate(rows):
        cx = X0 + slot * (i + 0.5)
        y = ymap(v)
        parts.append(f'<rect x="{_n(cx - bw / 2)}" y="{_n(min(y, z))}" width="{_n(bw)}" '
                     f'height="{_n(abs(z - y))}" fill="var(--link)" fill-opacity="0.85"/>')
        if i % stride == 0:
            parts.append(f'<text x="{_n(cx)}" y="{Y1 + 17}" text-anchor="middle">{_esc(lab)}</text>')
        if n <= 12:
            parts.append(f'<text x="{_n(cx)}" y="{_n(min(y, z) - 4)}" text-anchor="middle" '
                         f'fill="var(--text-strong)">{_fmt(v, dec)}</text>')
    if unit:
        parts.append(f'<text x="{X1}" y="{Y0 - 4}" text-anchor="end">{_esc(unit)}</text>')
    parts.append("</g></svg>")
    return "".join(parts)
