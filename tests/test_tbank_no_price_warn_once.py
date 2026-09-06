"""Позиция без цены в T-Bank повторяется каждый синк — WARNING только один раз."""
import logging

from app.services import tbank_service as ts
from app.services.tbank_service import TBankService


def _pos(figi: str) -> dict:
    return {
        "figi": figi,
        "ticker": "XXX",
        "instrumentType": "bond",
        "quantity": {"units": "1", "nano": 0},
        "averagePositionPrice": {"units": "0", "nano": 0},
        "currentPrice": {"units": "0", "nano": 0},
    }


def test_no_price_warns_once_then_debug(caplog):
    ts._no_price_warned.discard("FIGI_NOPRICE")
    with caplog.at_level(logging.DEBUG, logger="app.services.tbank_service"):
        for _ in range(3):
            assert TBankService._positions_to_items([_pos("FIGI_NOPRICE")]) == []
    msgs = [r for r in caplog.records if "No price for figi=FIGI_NOPRICE" in r.getMessage()]
    assert [r.levelno for r in msgs] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
