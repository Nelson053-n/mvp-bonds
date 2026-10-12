"""Общий mailer: письма не блокируют event loop и несут Message-ID.

До выноса в mailer сброс пароля и подтверждение email вызывали блокирующий
smtplib прямо из async-кода — воркер замирал на всю SMTP-сессию.
"""
import asyncio
import time

import pytest

import app.services.mailer as mailer
from app.config import settings
from app.services.auth_service import auth_service


class _SlowSMTP:
    sent: list = []
    delay = 0.3

    def __init__(self, host, port, timeout=None):
        time.sleep(self.delay)  # как медленный SMTP-сервер

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
        _SlowSMTP.sent.append((frm, to, msg))


@pytest.fixture
def smtp(monkeypatch):
    _SlowSMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _SlowSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "alert@bondai.ru")
    return _SlowSMTP.sent


async def test_send_does_not_block_event_loop(smtp) -> None:
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    assert await mailer.send_mail("u@example.com", "S", "B", "test") is True
    t.cancel()
    # За 0.3с SMTP-сессии тикер должен был отработать ~15 раз; при блокировке — 0.
    assert ticks >= 5


async def test_headers(smtp) -> None:
    await mailer.send_mail("u@example.com", "Тема", "Текст", "test")
    (frm, to, msg), = smtp
    assert to == ["u@example.com"]
    assert "Message-ID: <" in msg and "@bondai.ru>" in msg
    assert "\nDate: " in msg


async def test_not_configured(smtp, monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", "")
    assert await mailer.send_mail("u@example.com", "S", "B", "test") is False
    assert smtp == []


async def test_failure_returns_false(monkeypatch, smtp) -> None:
    class _Boom(_SlowSMTP):
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("down")
    monkeypatch.setattr(mailer.smtplib, "SMTP", _Boom)
    assert await mailer.send_mail("u@example.com", "S", "B", "test") is False


async def test_password_reset_email_goes_through_mailer(smtp) -> None:
    assert await auth_service._send_email_reset("u@example.com", "123456", "bob") is True
    (_, to, msg), = smtp
    assert to == ["u@example.com"]


async def test_change_email_sends_confirmation(client, auth_headers, smtp) -> None:
    r = await client.post("/auth/change-email", json={"email": "u@example.com"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["smtp_available"] is True
    assert [to for _, to, _ in smtp] == [["u@example.com"]]
