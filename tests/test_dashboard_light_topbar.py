"""Светлая тема кабинета: светлая шапка и монохромный пункт «Все портфели».

Шапка /app в светлой теме оставалась тёмной (--topbar-bg был rgba(15,23,42,.97)),
хотя публичные страницы (/bond) в светлой теме светлые. Пункт «Все портфели»
начинался с цветного эмодзи 📊 — в <option> его не перекрасить.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")


def _light_block() -> str:
    m = re.search(r'\n\[data-theme="light"\] \{\n(.*?)\n\}', CSS, re.S)
    assert m, "не найден блок [data-theme=light] с переменными"
    return m.group(1)


def test_light_theme_topbar_is_light() -> None:
    block = _light_block()
    bg = re.search(r"--topbar-bg:\s*rgba\((\d+),\s*(\d+),\s*(\d+)", block)
    assert bg, "в светлой теме не задан --topbar-bg"
    # тёмный фон шапки (15,23,42) на светлой теме — именно тот баг
    assert min(int(x) for x in bg.groups()) >= 200


def test_light_theme_topbar_text_not_white() -> None:
    # название бренда в тёмной теме жёстко белое — на светлой шапке нужен override
    assert re.search(r'\[data-theme="light"\] \.brand-name \{[^}]*color:\s*var\(--text-primary\)', CSS)
    assert re.search(r'\[data-theme="light"\] \.nav-btn \{[^}]*color:\s*var\(--text-secondary\)', CSS)


def test_all_portfolios_option_has_no_color_emoji() -> None:
    assert "📊 Все портфели" not in APP_JS
    # два места: десктопный и мобильный селектор
    assert APP_JS.count("'Σ  Все портфели'") == 2


def test_selector_width_counts_every_option() -> None:
    # ширина считалась только по именам портфелей — «Все портфели» обрезалось
    assert "Array.from(selector.options" in APP_JS
