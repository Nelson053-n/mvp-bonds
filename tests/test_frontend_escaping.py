"""Экранирование в интерфейсе (аудит 12.10.2026, M8 и LOW).

M8: manual_rating владельца портфеля уходил в innerHTML подсказки YTM-графика
на ПУБЛИЧНОЙ share-странице; JWT лежит в localStorage — XSS там = захват
аккаунта посетителя. Сейчас спасал только max_length=16.
"""
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import UpdateInstrumentInput

APP_JS = (Path(__file__).resolve().parent.parent / "app/ui/static/app.js").read_text(encoding="utf-8")


_BASE = {"quantity": 1, "purchase_price": 100}


@pytest.mark.parametrize("v", ["", "A", "AA-", "BBB+", "ВВВ-", "ruAA-", "AAA(RU)", "A+ / RU"])
def test_manual_rating_real_values_pass(v) -> None:
    """Все 106 значений с прода — буквы и + -; плюс типовые форматы агентств."""
    assert UpdateInstrumentInput(**_BASE, manual_rating=v).manual_rating == v


@pytest.mark.parametrize("v", ["<svg onload=x>", "<img src=x>", 'A"onmouseover', "A;b", "a&b"])
def test_manual_rating_markup_rejected(v) -> None:
    with pytest.raises(ValidationError) as exc:
        UpdateInstrumentInput(**_BASE, manual_rating=v)
    # Ошибка именно в manual_rating — а не в недостающем обязательном поле.
    assert [e["loc"] for e in exc.value.errors()] == [("manual_rating",)]


@pytest.mark.parametrize("snippet", [
    "${esc(ratingLabel)}",                       # подсказка YTM на share-странице
    "${esc(c.name||c.ticker||'—')}",             # карточка рекомендации
    "onclick=\"addRecommendedBond(this.dataset.ticker",  # не esc() внутри JS-строки
    "esc(s.last_error_msg)",                     # админка: ошибка источника
    'value="${esc(savedUsername)}"',
])
def test_app_js_escapes(snippet) -> None:
    assert snippet in APP_JS


def test_table_polling_stops_when_logged_out() -> None:
    """После 401 (флаг снят) вкладка не должна опрашивать /table раз в 15 минут."""
    i = APP_JS.index("syncTableFromServer().catch(() => {});\n            }, SYNC_INTERVAL_MS);")
    interval_body = APP_JS[APP_JS.rindex("setInterval(", 0, i):i]
    assert "localStorage.getItem('mvp_logged_in')" in interval_body


def test_ai_comment_uses_safe_dom_and_independent_position_state() -> None:
    block = APP_JS[APP_JS.index("function createInstrumentComment"):APP_JS.index("// ── TOAST", APP_JS.index("function createInstrumentComment"))]
    assert "value.textContent" in block
    assert "expandedInstrumentComments.has(key)" in block
    assert "aria-expanded" in block and "aria-controls" in block
    assert "innerHTML" not in block


def test_ai_comment_preview_has_absolute_140_character_limit() -> None:
    assert "INSTRUMENT_COMMENT_PREVIEW_LIMIT = 140" in APP_JS
    assert "Array.from(fullText.trim())" in APP_JS
    assert "createInstrumentComment(row.id, row.ai_comment)" in APP_JS
