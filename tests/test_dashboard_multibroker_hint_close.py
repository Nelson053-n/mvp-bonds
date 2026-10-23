"""Плашка «Ведёте несколько брокеров?» закрывается крестиком, выбор помнится в браузере 30 дней."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app/ui/dashboard.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def _hint() -> str:
    start = HTML.index('id="multibroker-hint"')
    return HTML[start:HTML.index("</div>", HTML.index('id="multibroker-hint-close"', start))]


def test_close_button_is_not_inside_the_link() -> None:
    hint = _hint()
    link = re.search(r"<a [^>]*href=\"/all\".*?</a>", hint, re.S).group(0)
    assert 'id="multibroker-hint-close"' not in link  # кнопка в <a> — невалидно и ведёт на /all
    assert re.search(r'<button[^>]*id="multibroker-hint-close"[^>]*aria-label="Закрыть"', hint)
    assert not re.match(r"<a ", HTML[HTML.index('id="multibroker-hint"') - 9:].lstrip())  # контейнер не ссылка


def test_dismissal_is_persisted_and_respected() -> None:
    assert re.search(r"getElementById\('multibroker-hint-close'\)\?\.addEventListener\('click'", APP_JS)
    # срок — 30 дней, а не навсегда: хранится момент, до которого плашка скрыта
    assert "localStorage.setItem(_MB_HINT_KEY, String(Date.now() + 30 * 86400 * 1000))" in APP_JS
    assert "Number(localStorage.getItem(_MB_HINT_KEY) || 0) > Date.now()" in APP_JS
    assert re.search(r"portfolios\.length > 1 && !_mbHintDismissed\(\)", APP_JS)
