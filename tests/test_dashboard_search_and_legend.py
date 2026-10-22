"""Кабинет: поиск по таблице не должен получать подсказки паролей Chrome,
легенда графика стоимости не должна лежать поверх линий.

Chrome считал #table-search «логином» формless-полей паролей из других панелей
(токены, смена пароля) и предлагал сохранённые учётки. Изоляция — собственная
<form> без полей пароля.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app/ui/dashboard.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def _search_form() -> str:
    m = re.search(r'<form[^>]*class="table-search"[^>]*>(.*?)</form>', HTML, re.S)
    assert m, "поиск таблицы не обёрнут в <form class=table-search>"
    return m.group(0)


def test_table_search_is_isolated_in_own_form() -> None:
    form = _search_form()
    assert 'id="table-search"' in form
    assert "type=\"password\"" not in form
    assert 'onsubmit="return false"' in form  # Enter не перезагружает страницу


def test_table_search_marked_as_not_a_login_field() -> None:
    form = _search_form()
    assert 'autocomplete="off"' in form
    assert 'data-lpignore="true"' in form and "data-1p-ignore" in form


def test_chart_legend_has_own_strip_above_plot() -> None:
    # раньше легенда рисовалась в pad.t + 10 + i*12 — внутри области графика
    assert "pad.t + 10 + i * 12" not in APP_JS
    assert "const legendLayout" in APP_JS
    assert re.search(r"pad = \{ t: Math\.max\(16, legendH \+ 12\)", APP_JS)
