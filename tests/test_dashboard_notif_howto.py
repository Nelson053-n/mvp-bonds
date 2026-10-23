"""Вкладка «Уведомления»: у обычного пользователя — инструкция по подключению
бота и переключатель купонов; поля Bot Token / Chat ID видит только админ.

Раньше обычный пользователь видел админскую форму токена, а привязка бота
лежала на другой вкладке (тикет #2 от 21.10: «как подключить бота?»).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "app/ui/dashboard.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
TR = (ROOT / "app/ui/static/translations.js").read_text(encoding="utf-8")


def _card(card_id: str) -> str:
    start = HTML.index(f'id="{card_id}"')
    return HTML[start:HTML.index("</div>\n            </div>", start)]


def test_user_card_has_steps_button_and_coupon_toggle() -> None:
    user = HTML[HTML.index('id="notif-user-card"'):HTML.index('id="notif-admin-card"')]
    assert "@bondinfoai_bot" in user
    assert 'id="notif-tg-btn"' in user and 'id="notif-tg-state"' in user
    assert 'id="coupon-notif-enabled"' in user  # раньше был спрятан в админской карточке


def test_token_fields_only_in_admin_card_hidden_by_default() -> None:
    user = HTML[HTML.index('id="notif-user-card"'):HTML.index('id="notif-admin-card"')]
    assert 'id="tg-token"' not in user and 'id="tg-chat"' not in user
    admin_tag = re.search(r'<div[^>]*id="notif-admin-card"[^>]*>', HTML).group(0)
    assert "display:none" in admin_tag
    assert "notif-admin-card" in APP_JS  # setupAdminNav показывает её админу


def test_button_reuses_account_linking_and_state_is_mirrored() -> None:
    assert re.search(r"notif-tg-btn'\)\?\.addEventListener\('click'.*?acc-tg-btn'\)\.click\(\)", APP_JS, re.S)
    assert "notif-tg-state" in APP_JS


def test_translations_in_both_languages() -> None:
    for key in ("howTitle", "step1", "step2", "step3", "step4", "stepNote"):
        assert TR.count(f"'settings.notif.{key}'") == 2, key
