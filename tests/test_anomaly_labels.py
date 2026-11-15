"""Anomaly notices must name the bond, not just the ticker."""
from app.api.portfolios import _instrument_label


def test_label_with_name_and_ticker() -> None:
    assert _instrument_label("RU000A10EEE4", "БалтлизПГ24") == "БалтлизПГ24 (RU000A10EEE4)"


def test_label_without_name() -> None:
    assert _instrument_label("RU000A10EEE4", "") == "RU000A10EEE4"
    assert _instrument_label("RU000A10EEE4", None) == "RU000A10EEE4"


def test_label_trims_whitespace() -> None:
    assert _instrument_label(" T ", "  Бонд  ") == "Бонд (T)"


def test_label_without_ticker() -> None:
    assert _instrument_label("", "Бонд") == "Бонд"