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


async def test_not_configured_skips(smtp, monkeypatch) -> None:
    monkeypatch.setattr(settings, "alert_email", "")
    assert await notification_service.send_alert_email("Тема", "Текст") is False
    assert smtp == []


async def _one_iteration(monkeypatch, probe_result: dict) -> tuple[list, list]:
    emails: list = []
    tgs: list = []

    async def probe():
        return probe_result

    async def fake_email(subject, body):
        emails.append((subject, body))
        return True

    async def fake_tg(token, chat, text):
        tgs.append(text)
        return True

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(telegram_net, "probe_routes", probe)
    monkeypatch.setattr(notification_service, "send_alert_email", fake_email)
    monkeypatch.setattr(notification_service, "send_telegram", fake_tg)
    monkeypatch.setattr(main_mod.asyncio, "sleep", stop)
    monkeypatch.setattr(main_mod.storage_service, "get_all_settings",
                        lambda: {"tg_bot_token": "T", "tg_chat_id": "1"})
    with pytest.raises(asyncio.CancelledError):
        await main_mod._telegram_routes_loop()
    return emails, tgs


async def test_all_routes_down_sends_email_not_telegram(monkeypatch) -> None:
    emails, tgs = await _one_iteration(
        monkeypatch, {"149.154.166.110": False, "149.154.167.220": False})
    assert len(emails) == 1 and "ни один маршрут" in emails[0][1]
    assert tgs == []


async def test_partial_outage_goes_to_telegram(monkeypatch) -> None:
    emails, tgs = await _one_iteration(
        monkeypatch, {"149.154.166.110": False, "149.154.167.220": True})
    assert emails == []
    assert len(tgs) == 1
