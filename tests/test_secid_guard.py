"""Тикер проверяется в moex_service — единой точке перед URL (аудит 12.10.2026, M7).

Проверка формата стояла только в AddInstrumentInput и обходилась CSV-импортом,
import-all, watchlist и кнопкой бота: «A/../../x?y» давал GET на произвольный
путь iss.moex.com / smart-lab.ru с IP прода. На проде все 2346 биржевых
позиций проходят новый шаблон.
"""
import pytest

from app.exceptions import PriceNotFoundError
from app.services.moex_service import MOEXService, valid_secid

BAD = ["A/../../q/other", "..", ".", "A?x=1", "A#frag", "A B", "SBER/../../x", "", "Ф"]
GOOD = ["SBER", "SU26238RMFS4", "RU000A106E90", "MGKL1P2", "BRK.B", "A-1_2"]


@pytest.mark.parametrize("t", GOOD)
def test_good_tickers_pass(t) -> None:
    assert valid_secid(t)


@pytest.mark.parametrize("t", BAD)
def test_bad_tickers_rejected(t) -> None:
    assert not valid_secid(t.upper().strip())


@pytest.fixture
def svc(monkeypatch):
    s = MOEXService()
    calls: list = []

    async def fake_fetch(url, source="moex_price"):
        calls.append(url)
        raise AssertionError("не должно быть запроса")
    monkeypatch.setattr(s, "_fetch", fake_fetch)

    class _Http:  # SmartLab ходит не через _fetch, а через общий клиент
        async def get(self, url, **kw):
            calls.append(url)
            raise AssertionError("не должно быть запроса")
    monkeypatch.setattr(s, "_get_http", lambda: _Http())
    s.sources["smartlab"].enabled = True
    s._calls = calls
    return s


@pytest.mark.parametrize("t", ["A/../../q/other", "..", "A?x=1"])
async def test_entry_points_do_not_fetch(svc, t) -> None:
    with pytest.raises(PriceNotFoundError):
        await svc.get_bond_snapshot(t)
    with pytest.raises(PriceNotFoundError):
        await svc.get_stock_snapshot(t)
    assert await svc.get_last_known_coupon(t) is None
    assert await svc._get_credit_rating(t) is None
    assert await svc._get_sec_meta(t) == (False, True)
    assert await svc._get_smartlab_credit_rating(t) is None
    assert svc._calls == []


async def test_bot_callback_with_bad_ticker_skips_moex(monkeypatch) -> None:
    from app.services import telegram_bot_service as tbs
    called: list = []

    async def fake_snap(*a, **k):
        called.append(a)
        raise AssertionError
    monkeypatch.setattr(tbs.moex_service, "get_bond_snapshot", fake_snap)
    bot = tbs.TelegramBotService()
    sent: list = []

    async def fake_send(client, chat_id, text, keyboard=None):
        sent.append(text)
    bot._send = fake_send

    async def fake_call(*a, **k):
        return {"ok": True}
    bot._call = fake_call
    await bot._handle_update(None, {"callback_query": {"id": "1", "data": "b:A/../../x",
                                                       "message": {"chat": {"id": 5}}}})
    assert called == [] and "не найдена" in sent[-1]
