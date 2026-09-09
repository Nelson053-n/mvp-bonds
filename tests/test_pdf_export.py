"""Экспорт портфеля в PDF.

Модуль был покрыт на 9%, и именно поэтому баг дожил до прода: 09.09 reportlab
отсутствовал в окружении, обе ветки _generate_pdf падали с ImportError, и
/portfolios/{id}/report.pdf отдавал 500 двум живым пользователям. Ни один тест
не дёргал этот путь.

Проверяем и генерацию (валидный PDF, кириллица, граничные данные), и HTTP-контур
(авторизация, чужой портфель, заголовки ответа).
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.api import pdf as pdf_module
from app.api.pdf import _find_cyrillic_font, _find_cyrillic_font_bold, _generate_pdf, _t


def _row(**overrides) -> SimpleNamespace:
    """Строка таблицы портфеля со всеми полями, которые читает генератор."""
    defaults = dict(
        ticker="SU26238RMFS4",
        name="ОФЗ 26238",
        quantity=10,
        purchase_price=550.0,
        current_price=555.0,
        current_value=5550.0,
        company_rating="AAA",
        market_yield=14.5,
        coupon_rate=7.1,
        maturity_date=date.today() + timedelta(days=3650),
        next_coupon_date=date.today() + timedelta(days=30),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# 14 стандартных шрифтов PDF: они присутствуют в любом документе и НЕ являются
# встроенными — reportlab только ссылается на них. Кириллический TTF, наоборот,
# встраивается с префиксом подмножества (AAAAAA+DejaVuSans).
_BASE14 = {
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Symbol", "ZapfDingbats",
}


def _embedded_fonts(pdf_bytes: bytes) -> set[str]:
    """Имена НЕбазовых шрифтов, реально встроенных в документ."""
    import re

    names = {f.decode() for f in re.findall(rb"/BaseFont\s*/([A-Za-z0-9+,-]+)", pdf_bytes)}
    return {n.split("+")[-1] for n in names} - _BASE14


# ── Генерация ────────────────────────────────────────────────────────────────

def test_generates_valid_pdf():
    data = _generate_pdf("Мой портфель", [_row()], "ru")
    assert data.startswith(b"%PDF-"), "ответ должен быть настоящим PDF"
    assert len(data) > 1000


def test_cyrillic_font_found_in_system():
    """Кириллический TTF обязан находиться — иначе отчёт молча уедет на ASCII.

    Скипать эту проверку нельзя: отсутствие шрифта И ЕСТЬ тот сбой, который
    она ловит (пропущенный тест выглядел бы как успех). Прод проверен —
    DejaVuSans лежит в /usr/share/fonts/truetype/dejavu/.
    """
    name, path = _find_cyrillic_font()
    assert path is not None, (
        "кириллический TTF не найден: PDF на русском выйдет нечитаемым"
    )
    assert name == "CyrillicFont"
    bold_name, bold_path = _find_cyrillic_font_bold()
    assert bold_path is not None
    assert bold_name == "CyrillicFontBold"


def test_cyrillic_font_embedded_into_pdf():
    """Шрифт должен попасть В ДОКУМЕНТ, а не только найтись на диске.

    Проверяем /BaseFont в самом PDF: reportlab встраивает TTF под его
    НАСТОЯЩИМ именем (DejaVuSans), а внутреннее имя регистрации
    ("CyrillicFont") в файл не попадает — искать его бесполезно.
    Признак ASCII-фолбэка: среди встроенных шрифтов только базовые.
    """
    data = _generate_pdf("Портфель с кириллицей", [_row(name="ОФЗ Тест")], "ru")
    embedded = _embedded_fonts(data)
    assert embedded, f"в PDF нет встроенных шрифтов — кириллица уехала на ASCII"


def test_ascii_fallback_is_detectable(monkeypatch):
    """Контрольный опыт: без TTF в документе остаются только базовые шрифты.

    Без этого теста предыдущий не доказывает ничего — надо убедиться, что
    его условие РАЗЛИЧАЕТ рабочий случай и сломанный.
    """
    monkeypatch.setattr(pdf_module, "_find_cyrillic_font", lambda: ("Helvetica", None))
    monkeypatch.setattr(
        pdf_module, "_find_cyrillic_font_bold", lambda: ("Helvetica-Bold", None)
    )
    data = _generate_pdf("Портфель", [_row(name="ОФЗ Тест")], "ru")
    assert not _embedded_fonts(data), "ожидался чистый ASCII-фолбэк"


def test_both_languages_supported():
    for lang in ("ru", "en"):
        data = _generate_pdf("Portfolio", [_row()], lang)
        assert data.startswith(b"%PDF-")


def test_empty_portfolio_still_renders():
    """Пустой портфель — не ошибка: отчёт должен построиться."""
    data = _generate_pdf("Пустой", [], "ru")
    assert data.startswith(b"%PDF-")


def test_missing_optional_values_do_not_crash():
    """Бумага без цены/рейтинга/дат (нет котировки, custom) не должна ронять отчёт."""
    row = _row(
        current_price=None, current_value=None, company_rating=None,
        market_yield=None, coupon_rate=None,
        maturity_date=None, next_coupon_date=None,
    )
    data = _generate_pdf("Портфель", [row], "ru")
    assert data.startswith(b"%PDF-")


def test_negative_pnl_renders():
    """Убыток — отдельная ветка форматирования итоговой строки."""
    data = _generate_pdf("Убыточный", [_row(current_price=100.0, current_value=1000.0)], "ru")
    assert data.startswith(b"%PDF-")


def test_many_rows_paginate():
    data = _generate_pdf("Большой", [_row(ticker=f"BOND{i:03d}") for i in range(60)], "ru")
    assert data.startswith(b"%PDF-")
    assert len(data) > 5000


def test_special_characters_in_name_do_not_break_pdf():
    """Имя портфеля идёт в разметку reportlab — угловые скобки надо пережить."""
    data = _generate_pdf("<b>Портфель</b> & \"кавычки\"", [_row()], "ru")
    assert data.startswith(b"%PDF-")


# ── Вспомогательные ──────────────────────────────────────────────────────────

def test_translation_falls_back_to_key():
    assert _t("ru", "title")
    assert _t("ru", "no_such_key_xyz") == "no_such_key_xyz"
    # Неизвестный язык обслуживается русским словарём, а не падает.
    assert _t("de", "title") == _t("ru", "title")


def test_font_lookup_returns_helvetica_when_nothing_found(monkeypatch):
    monkeypatch.setattr(pdf_module.os.path, "exists", lambda p: False)
    assert _find_cyrillic_font() == ("Helvetica", None)
    assert _find_cyrillic_font_bold() == ("Helvetica-Bold", None)


# ── HTTP-контур ──────────────────────────────────────────────────────────────

async def test_pdf_endpoint_requires_auth(client):
    resp = await client.get("/portfolios/1/report.pdf")
    assert resp.status_code in (401, 403)


async def test_pdf_endpoint_returns_file(client, auth_headers, monkeypatch, sample_bond_input):
    """Главный регресс: этот путь на проде отдавал 500 из-за отсутствия reportlab."""
    created = await client.post("/portfolios", json={"name": "PDF тест"}, headers=auth_headers)
    assert created.status_code in (200, 201)
    pid = created.json()["id"]

    async def fake_table(portfolio_id, *a, **kw):
        return [_row()]

    monkeypatch.setattr(pdf_module.portfolio_service, "get_table", fake_table)

    resp = await client.get(f"/portfolios/{pid}/report.pdf?lang=ru", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert f"portfolio_{pid}_" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")


async def test_pdf_endpoint_rejects_foreign_portfolio(client, auth_headers):
    """Чужой/несуществующий портфель не должен отдавать файл."""
    resp = await client.get("/portfolios/999999/report.pdf", headers=auth_headers)
    assert resp.status_code in (403, 404)


async def test_pdf_endpoint_validates_lang(client, auth_headers):
    resp = await client.get("/portfolios/1/report.pdf?lang=xx", headers=auth_headers)
    assert resp.status_code == 422
