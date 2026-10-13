"""Лимиты против злоупотреблений: регистрация (M9) и смена email (M4), аудит 12.10.2026."""
import pytest

from app.config import settings
from app.services.storage_service import storage_service


@pytest.fixture(autouse=True)
def _clean_limits():
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    yield


async def test_register_rate_limited_per_ip(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "register_max_per_hour", 3)
    codes = [(await client.post("/auth/register", json={"username": f"lim_user_{i}", "password": "secret12"})).status_code
             for i in range(4)]
    assert codes == [201, 201, 201, 429]


async def test_register_attempts_with_taken_name_count_too(client, monkeypatch) -> None:
    """Перебор занятых имён тоже упирается в лимит."""
    monkeypatch.setattr(settings, "register_max_per_hour", 2)
    await client.post("/auth/register", json={"username": "taken_one", "password": "secret12"})
    codes = [(await client.post("/auth/register", json={"username": "taken_one", "password": "secret12"})).status_code
             for _ in range(2)]
    assert codes[-1] == 429


class _SMTP:
    sent: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, *a):
        pass

    def sendmail(self, frm, to, msg):
        _SMTP.sent.append(to)


@pytest.fixture
def smtp(monkeypatch):
    import app.services.mailer as mailer
    _SMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "alert@bondai.ru")
    return _SMTP.sent


async def test_change_email_sends_only_on_change(client, auth_headers, smtp) -> None:
    for _ in range(3):
        r = await client.post("/auth/change-email", json={"email": "same@example.com"}, headers=auth_headers)
        assert r.status_code == 200
    assert smtp == [["same@example.com"]]


async def test_change_email_rate_limited(client, auth_headers, smtp) -> None:
    codes = [(await client.post("/auth/change-email", json={"email": f"a{i}@example.com"},
                                headers=auth_headers)).status_code for i in range(6)]
    assert codes == [200] * 5 + [429]
    assert len(smtp) == 5


@pytest.mark.parametrize("email,ok", [
    ("user@example.com", True), ("", True),
    ("not-an-email", False), ("a@b", False), ("x@y.z, evil@ex.com", False), ("a b@c.d", False),
])
async def test_change_email_format(client, auth_headers, smtp, email, ok) -> None:
    r = await client.post("/auth/change-email", json={"email": email}, headers=auth_headers)
    assert (r.status_code == 200) is ok, (email, r.status_code)
