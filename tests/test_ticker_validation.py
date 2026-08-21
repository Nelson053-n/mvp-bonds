"""Тикер биржевой бумаги подставляется в URL внешних API — формат обязан
проверяться. Регрессия (security-review 21.08): поле было ограничено только
длиной, поэтому «/», «..» и «?» пролезали в путь запроса к
iss.moex.com/.../securities/{secid}.json и smart-lab.ru/q/bonds/{secid}/.
Хост подменить было нельзя, но путь и query становились управляемыми.
"""
import pytest
from pydantic import ValidationError

from app.models import AddInstrumentInput


def _payload(ticker: str, **kw) -> dict:
    base = {"ticker": ticker, "quantity": 10.0, "purchase_price": 100.0}
    base.update(kw)
    return base


class TestExchangeTicker:
    @pytest.mark.parametrize("ticker", [
        "SBER",
        "RU000A105G99",
        "SU26238RMFS4",
        "MGKL1P2",
        "TCS-A.1_x",       # точка, дефис, подчёркивание допустимы
    ])
    def test_valid_tickers_accepted(self, ticker: str) -> None:
        assert AddInstrumentInput(**_payload(ticker)).ticker == ticker

    @pytest.mark.parametrize("ticker", [
        "../../admin",              # выход из каталога
        "X/../../etc/passwd",       # то же со слэшем внутри
        "SBER?iss.only=x",          # подмена query-параметров
        "SBER/../other",
        "SBER json",                # пробел
        "SBER#frag",
        "SBER%2f..%2f",             # процент-кодирование
    ])
    def test_path_injection_rejected(self, ticker: str) -> None:
        with pytest.raises(ValidationError):
            AddInstrumentInput(**_payload(ticker))


class TestCustomTicker:
    """У внебиржевых бумаг «тикер» — произвольная подпись, в MOEX не уходит."""

    @pytest.mark.parametrize("ticker", [
        "ТАК СЕБЕ",
        "МОЯ ВНЕБИРЖЕВАЯ",
        "ГАЗПРОМ НЕФТЬ-006Р-02R",
        "ОФЗ 29007",
        "ЛИКВИДНОСТЬ",
    ])
    def test_real_prod_custom_labels_still_accepted(self, ticker: str) -> None:
        """Реальные подписи с прода — строгий pattern сломал бы 6 живых позиций."""
        payload = _payload(ticker, is_custom=True, instrument_type="bond")
        assert AddInstrumentInput(**payload).ticker == ticker

    def test_custom_flag_is_what_relaxes_check(self) -> None:
        """Та же подпись без is_custom отвергается — послабление даёт именно флаг."""
        with pytest.raises(ValidationError):
            AddInstrumentInput(**_payload("ТАК СЕБЕ"))
