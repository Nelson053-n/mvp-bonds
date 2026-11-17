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


def test_ai_comment_popover_uses_safe_text_and_accessible_relationship() -> None:
    block = APP_JS[APP_JS.index("function installAiCommentPopover"):APP_JS.index("// ── TOAST", APP_JS.index("function installAiCommentPopover"))]
    assert "aiCommentPopover.textContent = text" in block
    assert "aria-describedby" in block
    assert "role', 'tooltip" in block
    assert "restoreFocus: true" in block
    assert "aiCommentPopover.innerHTML" not in block


def test_ai_comment_action_is_conditional_and_rendered_in_action_cell() -> None:
    block = APP_JS[APP_JS.index("function createAiCommentAction"):APP_JS.index("// ── TOAST", APP_JS.index("function createAiCommentAction"))]
    assert "String(fullText || '').trim()" in block
    assert "if (!text) return null" in block
    assert "createAiCommentAction(row.id, row.ai_comment" in APP_JS
    assert "actionGroup.prepend(aiAction)" in APP_JS
    assert "createInstrumentComment" not in APP_JS
    assert ".instrument-comment" not in APP_JS


def test_ai_comment_popover_has_hover_focus_tap_and_viewport_positioning() -> None:
    assert "button.addEventListener('pointerenter'" in APP_JS
    assert "button.addEventListener('focus'" in APP_JS
    assert "aiCommentMode === 'tap'" in APP_JS
    assert "window.addEventListener('resize', queuePosition)" in APP_JS
    assert "window.addEventListener('scroll', queuePosition, true)" in APP_JS
    assert "if (!isAllMode)" in APP_JS
