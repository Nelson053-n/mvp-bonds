import ast
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.api import ofz_charts
from app.api.ofz_charts import bars_chart, curve_chart, line_chart, spread_chart

FIXTURE = Path(__file__).parent / "fixtures" / "moex" / "hist_SU26238RMFS4.json"
COLOR_OK = re.compile(r"^(none|var\(--[a-z0-9-]+\)|url\(#[\w-]+\))$")


@pytest.fixture(scope="module")
def real_series():
    rows = json.loads(FIXTURE.read_text())["history"]["data"]
    return [(r[0], r[1]) for r in rows]


@pytest.fixture(scope="module")
def long_series(real_series):
    # реальный ряд фикстуры ровно 100 точек; 250 нужно для бюджета размера,
    # поэтому значения берутся по кругу, а даты идут подряд.
    start = date(2025, 1, 1)
    return [((start + timedelta(days=i)).isoformat(), real_series[i % len(real_series)][1])
            for i in range(250)]


def _all_charts(series):
    spread = [(d, (v - 7.6) * 100) for d, v in series]
    nodes = [(0.25, 7.0), (1, 7.5), (2, 8.0), (5, 8.5), (8, 8.8)]
    pts = [{"secid": f"SU{26000 + i}", "d_years": 0.15 * i, "y": 8 + 0.01 * i} for i in range(50)]
    return {
        "line": line_chart(series, title="Доходность", band=(7.5, 8.0), median=7.7),
        "spread": spread_chart(spread, median=10),
        "curve": curve_chart(nodes, pts, {"secid": "SU26238", "d_years": 5.2, "y": 8.4}),
        "bars": bars_chart([("2027", 100.0), ("2028", 50.0), ("2029", 0.0)], title="Амортизация", unit="₽"),
    }


def test_valid_xml_on_real_data(real_series):
    for name, svg in _all_charts(real_series).items():
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg"), name
        assert root.attrib["viewBox"] == "0 0 640 260"
        assert "width" not in root.attrib and "height" not in root.attrib
        assert "width:100%;height:auto" in root.attrib["style"]
        assert root.attrib["role"] == "img" and root.attrib["aria-label"]


def test_colors_only_via_css_variables(real_series):
    for name, svg in _all_charts(real_series).items():
        assert "rgb" not in svg and "#" not in re.sub(r'(href|clip-path|id)="[^"]*"', "", svg), name
        for el in ET.fromstring(svg).iter():
            for attr in ("fill", "stroke"):
                if attr in el.attrib:
                    assert COLOR_OK.match(el.attrib[attr]), (name, attr, el.attrib[attr])


def test_size_budget_250_points(long_series):
    charts = _all_charts(long_series)
    for name in ("line", "spread", "bars"):
        assert len(charts[name].encode()) <= 4096, name
    # 50 кружков с <title> тикера на кривой: отдельный бюджет
    assert len(charts["curve"].encode()) <= 6144


def test_empty_and_single_point_give_stub():
    for svg in (line_chart([], title="T"), line_chart([("2025-01-01", 7.0)], title="T"),
                spread_chart([], median=0), bars_chart([], title="T", unit=""),
                curve_chart([], [], None), line_chart([("мусор", "x")], title="T")):
        ET.fromstring(svg)
        assert "Недостаточно данных" in svg


def test_escaping(real_series):
    evil = "<script>alert(1)</script>"
    svgs = [
        line_chart(real_series, title=evil, unit=evil),
        bars_chart([(evil, 1.0), ("b", 2.0)], title=evil, unit=evil),
        curve_chart([(0, 7), (8, 8)], [{"label": evil, "d_years": 1, "y": 7.5}],
                    {"label": evil, "d_years": 2, "y": 7.6}),
    ]
    for svg in svgs:
        ET.fromstring(svg)
        assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_last_point_present(real_series, long_series):
    svg = line_chart(real_series, title="T", decimals=2)
    last = real_series[-1][1]
    assert f"{last:.2f}".replace(".", ",") + "%" in svg
    # даже после прореживания 250 точек последняя лежит на правом краю области
    root = ET.fromstring(line_chart(long_series, title="T"))
    ns = "{http://www.w3.org/2000/svg}"
    big = [c for c in root.iter(ns + "circle") if c.attrib["r"] == "4"]
    assert len(big) == 1 and float(big[0].attrib["cx"]) == ofz_charts.X1
    path = [p for p in root.iter(ns + "path") if p.attrib.get("stroke") == "var(--link)"][0]
    assert path.attrib["d"].endswith(f"{ofz_charts.X1} " + big[0].attrib["cy"])


def test_deterministic(real_series):
    assert _all_charts(real_series) == _all_charts(real_series)


def test_curve_drops_points_beyond_x_max():
    nodes = [(0.25, 7.0), (8, 8.0)]
    pts = [{"secid": "A", "d_years": 1, "y": 7.5}, {"secid": "B", "d_years": 9, "y": 8.0},
           {"secid": "C", "d_years": 8, "y": 8.0}]
    root = ET.fromstring(curve_chart(nodes, pts, {"secid": "H", "d_years": 12, "y": 8}, x_max=8.0))
    ns = "{http://www.w3.org/2000/svg}"
    radii = [c.attrib["r"] for c in root.iter(ns + "circle")]
    assert radii.count("3") == 2 and "6" not in radii


def test_curve_highlight_has_dashed_vertical():
    svg = curve_chart([(0.25, 7.0), (8, 8.0)], [], {"label": "X", "d_years": 3, "y": 7.5})
    assert 'r="6"' in svg and "stroke-dasharray" in svg


def test_spread_is_neutral_and_has_zero_line(real_series):
    svg = spread_chart([(d, (v - 7.6) * 100) for d, v in real_series], median=10)
    assert "--red" not in svg and "--green" not in svg
    assert svg.count("<use") == 2


def test_axis_dates_in_russian_months(real_series):
    svg = line_chart(real_series, title="T")
    assert re.search(r"(июл|авг|сен|окт|ноя) 21", svg)


def test_py310_syntax():
    src = Path(ofz_charts.__file__).read_text()
    ast.parse(src, feature_version=(3, 10))
