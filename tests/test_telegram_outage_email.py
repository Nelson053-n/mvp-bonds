"""Письмо админу, когда Telegram недоступен по всем маршрутам.

Алерт через сам Telegram в этот момент не доставить — остаётся почта.
"""
import asyncio
import logging

import pytest

import app.main as main_mod
import app.services.notification_service as ns_mod
from app.config import settings
from app.services import telegram_net
from app.services.notification_service import notification_service


class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.addr = (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, frm, to, msg):
        _FakeSMTP.sent.append((self.addr, frm, to, msg))


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(ns_mod.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.beget.com")
    monkeypatch.setattr(settings, "smtp_port", 2525)
    monkeypatch.setattr(settings, "smtp_from", "alerts@bondai.ru")
    monkeypatch.setattr(settings, "alert_email", "admin@example.com")
    return _FakeSMTP.sent


async def test_alert_email_sent(smtp) -> None:
    assert await notification_service.send_alert_email("Тема", "Текст") is True
    (addr, frm, to, msg), = smtp
    assert addr == ("smtp.beget.com", 2525)
    assert frm == "alerts@bondai.ru" and to == ["admin@example.com"]
    # Без Message-ID Gmail отбивает: 550 5.7.1 (так и было на проде).
    assert "Message-ID: <" in msg and "@bondai.ru>" in msg
    assert "\nDate: " in msg


async def test_not_configured_skips(smtp, monkeypatch) -> None:
    monkeypatch.setattr(settings, "alert_email", "")
    assert await notification_service.send_alert_email("Тема", "Текст") is False
    assert smtp == []


DOWN = {"149.154.166.110": False, "149.154.167.220": True}
UP = {"149.154.166.110": True, "149.154.167.220": True}
ALL_DOWN = {"149.154.166.110": False, "149.154.167.220": False}


async def _run(monkeypatch, probes: list[dict], store: dict | None = None):
    """Прогнать цикл по списку замеров; store — подмена app_settings."""
    store = {} if store is None else store
    emails: list = []
    tgs: list = []
    seq = iter(probes)

    async def probe():
        return next(seq)

    async def fake_email(subject, body):
        emails.append((subject, body))
        return True

    async def fake_tg(token, chat, text):
        tgs.append(text)
        return True

    calls = {"n": 0}

    async def sleep(_):
        calls["n"] += 1
        if calls["n"] >= len(probes):
            raise asyncio.CancelledError

    st = main_mod.storage_service
    monkeypatch.setattr(telegram_net, "probe_routes", probe)
    monkeypatch.setattr(notification_service, "send_alert_email", fake_email)
    monkeypatch.setattr(notification_service, "send_telegram", fake_tg)
    monkeypatch.setattr(main_mod.asyncio, "sleep", sleep)
    monkeypatch.setattr(st, "get_all_settings", lambda: {"tg_bot_token": "T", "tg_chat_id": "1"})
    monkeypatch.setattr(st, "get_setting", lambda k, d="": store.get(k, d))
    monkeypatch.setattr(st, "set_setting", lambda k, v: store.__setitem__(k, v))
    with pytest.raises(asyncio.CancelledError):
        await main_mod._telegram_routes_loop()
    return emails, tgs, store


async def test_all_routes_down_sends_email_not_telegram(monkeypatch) -> None:
    emails, tgs, _ = await _run(monkeypatch, [ALL_DOWN, ALL_DOWN])
    assert len(emails) == 1 and "ни один маршрут" in emails[0][1]
    assert tgs == []


async def test_partial_outage_goes_to_telegram(monkeypatch) -> None:
    emails, tgs, _ = await _run(monkeypatch, [DOWN, DOWN])
    assert emails == []
    assert len(tgs) == 1


async def test_single_blip_is_silent(monkeypatch) -> None:
    """Мигание на один замер — как у 149.154.166.110 на проде — не алертим."""
    emails, tgs, _ = await _run(monkeypatch, [UP, DOWN, UP, ALL_DOWN, UP])
    assert emails == [] and tgs == []


async def test_down_then_recovery_two_alerts(monkeypatch) -> None:
    _, tgs, _ = await _run(monkeypatch, [DOWN, DOWN, DOWN, UP, UP])
    assert len(tgs) == 2
    assert "недоступны" in tgs[0] and "снова доступны" in tgs[1]


async def test_restart_does_not_repeat_alert(monkeypatch) -> None:
    """После деплоя тот же сбой не объявляется заново."""
    _, tgs1, store = await _run(monkeypatch, [DOWN, DOWN])
    assert len(tgs1) == 1
    _, tgs2, _ = await _run(monkeypatch, [DOWN, DOWN, DOWN], store=store)
    assert tgs2 == []
