"""Tests for the public Telegram bond-search bot (logic only, no real Telegram)."""

from datetime import date, timedelta

import pytest

from app.models import BondSnapshot
from app.services import telegram_bot_service as tg
from app.services.moex_service import moex_service


_FAKE_BONDS = [
    {"ticker": "SU26238RMFS4", "name": "ОФЗ 26238", "price": 55.5, "market_yield": 14.5,
     "maturity": "2036-05-15", "board": "TQOB", "coupon_percent": 7.1, "rating": "AAA"},
    {"ticker": "SU26240RMFS0", "name": "ОФЗ 26240", "price": 60.1, "market_yield": 14.2,
     "maturity": "2036-07-30", "board": "TQOB", "coupon_percent": 7.0, "rating": "AAA"},
    {"ticker": "RU000A106K43", "name": "Сбербанк БО-1", "price": 98.0, "market_yield": 12.0,
     "maturity": "2028-01-01", "board": "TQCB", "coupon_percent": 11.5, "rating": "AAA"},
]


def _snapshot(**over) -> BondSnapshot:
    today = date.today()
    d = dict(
        ticker="SU26238RMFS4", name="ОФЗ 26238", clean_price_percent=55.5,
        nominal=1000.0, coupon=35.4, coupon_period=182, coupon_rate=7.1,
        maturity_date=today + timedelta(days=3650),
        next_coupon_date=today + timedelta(days=30),
        aci=12.34, market_yield=14.5, company_rating="AAA", face_unit="SUR",
    )
    d.update(over)
    return BondSnapshot(**d)


@pytest.fixture(autouse=True)
def _stub_bonds(monkeypatch):
    async def fake_cached():
        return list(_FAKE_BONDS)
    # patch the lazily-imported bonds module function
    import app.api.bonds as bonds_api
    monkeypatch.setattr(bonds_api, "_get_bonds_cached", fake_cached)
    yield


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_exact_ticker_first():
    res = await tg._search("SU26240RMFS0")
    assert res[0]["ticker"] == "SU26240RMFS0"


async def test_search_by_name_substring():
    res = await tg._search("Сбер")
    assert any(b["ticker"] == "RU000A106K43" for b in res)


async def test_search_partial_ticker_multiple():
    res = await tg._search("SU2624")
    assert {b["ticker"] for b in res} == {"SU26240RMFS0"}


async def test_search_no_match():
    assert await tg._search("ZZZNOTREAL") == []


# ── card formatting ──────────────────────────────────────────────────────────

def test_card_text_has_key_fields():
    txt = tg._card_text(_snapshot(), board="TQOB")
    assert "ОФЗ 26238" in txt
    assert "SU26238RMFS4" in txt
    assert "14,50%" in txt          # YTM
    assert "7,10%" in txt           # coupon
    assert "AAA" in txt
    assert "ОФЗ" in txt             # kind


def test_card_text_yield_to_offer_caveat():
    txt = tg._card_text(
        _snapshot(market_yield=2293.0, offer_date=date.today() + timedelta(days=20)),
        board="TQCB",
    )
    assert "к оферте" in txt
    assert "не к погашению" in txt


def test_card_text_escapes_html():
    txt = tg._card_text(_snapshot(name="<script>x</script>"), board="TQOB")
    assert "<script>x</script>" not in txt
    assert "&lt;script&gt;" in txt


def test_card_keyboard_links_to_site():
    kb = tg._card_keyboard("SU26238RMFS4")
    url = kb["inline_keyboard"][0][0]["url"]
    assert url == "https://bondai.ru/bond/SU26238RMFS4"


def test_choices_keyboard_uses_callback():
    kb = tg._choices_keyboard(_FAKE_BONDS)
    btns = kb["inline_keyboard"]
    assert all(row[0]["callback_data"].startswith("b:") for row in btns)
    assert btns[0][0]["callback_data"] == "b:SU26238RMFS4"


# ── update routing (capture outbound sends) ──────────────────────────────────

class _Recorder:
    """Stands in for the bot; records what would be sent instead of calling Telegram."""
    def __init__(self):
        self.sent = []
        self.cards = []

    async def send(self, client, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))

    async def card(self, client, chat_id, secid):
        self.cards.append((chat_id, secid))


@pytest.fixture
def bot(monkeypatch):
    b = tg.TelegramBotService()
    rec = _Recorder()
    monkeypatch.setattr(b, "_send", rec.send)
    monkeypatch.setattr(b, "_answer_card", rec.card)
    b._rec = rec
    return b


async def test_start_command(bot):
    await bot._handle_update(None, {"message": {"chat": {"id": 1}, "text": "/start"}})
    assert bot._rec.sent
    assert "Bond AI" in bot._rec.sent[0][1]


async def test_help_command(bot):
    await bot._handle_update(None, {"message": {"chat": {"id": 1}, "text": "/help"}})
    assert "тикер" in bot._rec.sent[0][1].lower()


async def test_exact_ticker_yields_card(bot):
    await bot._handle_update(None, {"message": {"chat": {"id": 7}, "text": "SU26238RMFS4"}})
    assert bot._rec.cards == [(7, "SU26238RMFS4")]


async def test_ambiguous_query_offers_choices(bot):
    await bot._handle_update(None, {"message": {"chat": {"id": 9}, "text": "ОФЗ"}})
    # no card; a choices message with an inline keyboard
    assert not bot._rec.cards
    chat, text, kb = bot._rec.sent[-1]
    assert chat == 9
    assert kb and "inline_keyboard" in kb


async def test_no_match_message(bot):
    await bot._handle_update(None, {"message": {"chat": {"id": 3}, "text": "ZZZNOPE"}})
    assert "ничего не нашёл" in bot._rec.sent[-1][1]


async def test_callback_button_yields_card(bot):
    async def noop(client, method, **kw):
        return {"ok": True}
    bot._call = noop  # swallow answerCallbackQuery
    await bot._handle_update(None, {
        "callback_query": {"id": "x", "data": "b:SU26240RMFS0",
                           "message": {"chat": {"id": 5}}},
    })
    assert bot._rec.cards == [(5, "SU26240RMFS0")]


async def test_disabled_without_token(monkeypatch):
    monkeypatch.setattr(tg.settings, "tg_bot_token", "")
    assert tg.TelegramBotService().enabled is False
    # run_polling must return immediately when disabled
    await tg.TelegramBotService().run_polling()
