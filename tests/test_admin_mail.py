"""Встроенный почтовый клиент админки: разбор писем, IMAP-синхронизация, ответ, права."""
import re
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.config import settings
from app.services import mail_client
from app.services.storage_service import storage_service

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with storage_service._connect() as conn:
        conn.execute("DELETE FROM mail_messages")
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'mail_imap_%'")
        conn.execute("DELETE FROM rate_limits")
        conn.commit()
    monkeypatch.setattr(settings, "mail_user", "support@bondai.ru")
    monkeypatch.setattr(settings, "mail_password", "pw")
    monkeypatch.setattr(settings, "mail_from", "support@bondai.ru")
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    yield


def _raw(subject="Вопрос", body="Как подключить бота?", frm="Иван <ivan@example.com>", mid="<a1@example.com>",
         html=None, attach=None) -> bytes:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Message-ID"] = subject, frm, "support@bondai.ru", mid
    m["Date"] = "Wed, 22 Oct 2026 10:00:00 +0000"
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    if attach:
        m.add_attachment(b"x", maintype="application", subtype="pdf", filename=attach)
    return m.as_bytes()


class FakeImap:
    """Минимальный IMAP4_SSL: INBOX с заданными UID."""
    mails: dict[int, bytes] = {}
    validity = b"7"
    fetched: list[int] = []

    def __init__(self, *a, **k): ...
    def login(self, u, p): return "OK", []
    def select(self, box, readonly=False): return "OK", [b"1"]
    def response(self, name): return name, [self.validity]
    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.mails)]
        uid = int(args[0]); FakeImap.fetched.append(uid)
        assert "PEEK" in args[1]  # не помечаем письма прочитанными на сервере
        return "OK", [(b"1 (BODY[] {1}", self.mails[uid]), b")"]
    def logout(self): ...


@pytest.fixture
def imap(monkeypatch):
    FakeImap.mails, FakeImap.fetched, FakeImap.validity = {}, [], b"7"
    monkeypatch.setattr(mail_client.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_parse_plain_html_and_attachment() -> None:
    p = mail_client.parse_message(_raw())
    assert p["body"] == "Как подключить бота?" and p["msg_id"] == "<a1@example.com>"
    assert p["date"].startswith("2026-10-22T10:00:00")
    h = mail_client.parse_message(_raw(html="<p>Привет<br>мир</p><script>x()</script><b>&amp;</b>"))
    assert "<" not in h["body"] and "x()" not in h["body"] and "&" in h["body"]
    a = mail_client.parse_message(_raw(attach="doc.pdf"))
    assert "doc.pdf" in a["body"]


def test_add_mail_deduplicates() -> None:
    kw = dict(from_addr="a@b.c", to_addr="s@b.c", subject="s", body="b", date="2026-10-22T10:00:00+00:00")
    assert storage_service.add_mail("in", "<x>", **kw)
    assert storage_service.add_mail("in", "<x>", **kw) is None
    assert storage_service.add_mail("out", "<x>", **kw)  # другое направление — другое письмо


async def test_sync_imports_new_only_and_is_idempotent(imap) -> None:
    imap.mails = {5: _raw(mid="<m5@x>"), 6: _raw(subject="Второе", mid="<m6@x>")}
    assert await mail_client.sync_inbox() == 2
    imap.mails[7] = _raw(subject="Третье", mid="<m7@x>")
    imap.fetched.clear()
    assert await mail_client.sync_inbox() == 1
    assert imap.fetched == [7]  # старые не тянем повторно
    assert [m["subject"] for m in storage_service.list_mail("in")][0] in {"Третье", "Второе"}
    assert storage_service.count_unread_mail() == 3


async def test_sync_resets_cursor_on_uidvalidity_change(imap) -> None:
    imap.mails = {5: _raw(mid="<m5@x>")}
    await mail_client.sync_inbox()
    imap.validity, imap.mails = b"8", {1: _raw(subject="Новый ящик", mid="<n1@x>")}
    assert await mail_client.sync_inbox() == 1  # UID 1 < прежнего 5, но validity другой


class FakeSmtp:
    sent: list = []

    def __init__(self, *a, **k): ...
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): ...
    def starttls(self, context=None): ...
    def login(self, u, p): ...
    def sendmail(self, frm, rcpt, text): FakeSmtp.sent.append((frm, rcpt, text))


@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.sent = []
    monkeypatch.setattr(mail_client.smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


async def test_reply_sets_threading_and_lands_in_outbox(client, auth_headers, smtp) -> None:
    orig = storage_service.add_mail("in", "<orig@x>", "Иван <ivan@example.com>", "support@bondai.ru",
                                    "Вопрос", "текст", "2026-10-22T10:00:00+00:00")
    r = await client.post("/admin/mail/send", headers=auth_headers, json={
        "to": "ivan@example.com", "cc": "boss@example.com", "subject": "Re: Вопрос", "body": "Ответ", "reply_to_id": orig})
    assert r.status_code == 200
    frm, rcpt, text = smtp.sent[0]
    assert frm == "support@bondai.ru" and rcpt == ["ivan@example.com", "boss@example.com"]
    assert "In-Reply-To: <orig@x>" in text
    assert "From: BondAI <support@bondai.ru>" in text
    assert frm == "support@bondai.ru"  # в конверте адрес без имени
    out = storage_service.list_mail("out")
    assert len(out) == 1 and out[0]["to_addr"] == "ivan@example.com"
    assert storage_service.get_mail(orig)["answered"] == 1  # входящее помечено отвеченным


@pytest.mark.parametrize("payload", [
    {"to": "ivan@example.com\nBcc: x@y.z", "subject": "s", "body": "b"},
    {"to": "не-адрес", "subject": "s", "body": "b"},
    {"to": "a@b.cc", "subject": "s\nBcc: x@y.z", "body": "b"},
    {"to": "a@b.cc", "cc": "плохой", "subject": "s", "body": "b"},
])
async def test_send_rejects_header_injection_and_bad_addresses(client, auth_headers, smtp, payload) -> None:
    r = await client.post("/admin/mail/send", headers=auth_headers, json=payload)
    assert r.status_code == 400 and not smtp.sent


async def test_send_requires_configuration(client, auth_headers, smtp, monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_password", "")
    r = await client.post("/admin/mail/send", headers=auth_headers, json={"to": "a@b.cc", "subject": "s", "body": "b"})
    assert r.status_code == 409 and not smtp.sent


async def test_smtp_failure_gives_502_and_nothing_in_outbox(client, auth_headers, monkeypatch) -> None:
    def boom(*a, **k): raise OSError("down")
    monkeypatch.setattr(mail_client.smtplib, "SMTP", boom)
    r = await client.post("/admin/mail/send", headers=auth_headers, json={"to": "a@b.cc", "subject": "s", "body": "b"})
    assert r.status_code == 502 and storage_service.list_mail("out") == []


async def test_non_admin_and_anonymous_are_refused(client) -> None:
    from app.services.auth_service import AuthService
    uid = storage_service.create_user("mail_plain_user", "x")
    token = AuthService().create_token(user_id=uid, username="mail_plain_user", is_admin=False)
    for headers, code in (({"Authorization": f"Bearer {token}"}, 403), ({}, 403)):
        for method, url in (("get", "/admin/mail"), ("get", "/admin/mail/1"), ("post", "/admin/mail/sync")):
            r = await getattr(client, method)(url, headers=headers)
            assert r.status_code == code, (method, url)
        r = await client.post("/admin/mail/send", headers=headers, json={"to": "a@b.cc", "subject": "s", "body": "b"})
        assert r.status_code == code


async def test_list_read_flag_and_unread_counter(client, auth_headers) -> None:
    mid = storage_service.add_mail("in", "<u1>", "a@b.cc", "s@b.cc", "Тема", "тело", "2026-10-22T10:00:00+00:00")
    r = await client.get("/admin/mail?direction=in", headers=auth_headers)
    assert r.json()["unread"] == 1 and r.json()["items"][0]["snippet"] == "тело"
    r = await client.patch(f"/admin/mail/{mid}", headers=auth_headers, json={"read": True})
    assert r.json()["unread"] == 0
    assert (await client.get("/admin/mail/999999", headers=auth_headers)).status_code == 404


def test_ui_has_two_tabs_and_renders_body_as_text() -> None:
    html = (ROOT / "app/ui/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    assert "adminShowTab('mail-in'" in html and "adminShowTab('mail-out'" in html
    assert 'data-adm-tab="mail-in"' in html and 'data-adm-tab="mail-out"' in html
    block = js[js.index("// ── Почта поддержки"):js.index("async function adminToggleReceipt")]
    assert "innerHTML" not in block  # тело письма из внешнего мира — только textContent
    assert re.search(r"mail-in\|mail-out\|promo", js)


def test_system_mailer_from_has_display_name(monkeypatch) -> None:
    from app.services import mailer
    monkeypatch.setattr(settings, "smtp_from", "alert@bondai.ru")
    monkeypatch.setattr(settings, "smtp_user", "alert@bondai.ru")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.sent = []
    mailer._send_sync("u@example.com", "Тема", "Тело")
    frm, rcpt, text = FakeSmtp.sent[0]
    assert "From: BondAI <alert@bondai.ru>" in text and frm == "alert@bondai.ru"


async def test_plain_send_does_not_mark_anything_answered(client, auth_headers, smtp) -> None:
    other = storage_service.add_mail("in", "<o>", "a@b.cc", "s@b.cc", "t", "b", "2026-10-22T10:00:00+00:00")
    await client.post("/admin/mail/send", headers=auth_headers, json={"to": "a@b.cc", "subject": "s", "body": "b"})
    assert storage_service.get_mail(other)["answered"] == 0


async def test_failed_send_does_not_mark_answered(client, auth_headers, monkeypatch) -> None:
    orig = storage_service.add_mail("in", "<f>", "a@b.cc", "s@b.cc", "t", "b", "2026-10-22T10:00:00+00:00")
    def boom(*a, **k): raise OSError("down")
    monkeypatch.setattr(mail_client.smtplib, "SMTP", boom)
    r = await client.post("/admin/mail/send", headers=auth_headers,
                          json={"to": "a@b.cc", "subject": "s", "body": "b", "reply_to_id": orig})
    assert r.status_code == 502 and storage_service.get_mail(orig)["answered"] == 0


async def test_delete_mail_removes_it_and_updates_counter(client, auth_headers) -> None:
    mid = storage_service.add_mail("in", "<d>", "a@b.cc", "s@b.cc", "t", "b", "2026-10-22T10:00:00+00:00")
    r = await client.delete(f"/admin/mail/{mid}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["unread"] == 0
    assert storage_service.get_mail(mid) is None
    assert (await client.delete(f"/admin/mail/{mid}", headers=auth_headers)).status_code == 404


async def test_deleted_mail_is_not_reimported_by_sync(imap) -> None:
    imap.mails = {5: _raw(mid="<m5@x>")}
    await mail_client.sync_inbox()
    storage_service.delete_mail(storage_service.list_mail("in")[0]["id"])
    assert await mail_client.sync_inbox() == 0 and storage_service.list_mail("in") == []


async def test_delete_requires_admin(client) -> None:
    from app.services.auth_service import AuthService
    uid = storage_service.create_user("mail_del_user", "x")
    token = AuthService().create_token(user_id=uid, username="mail_del_user", is_admin=False)
    r = await client.delete("/admin/mail/1", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_ui_has_back_delete_and_answered_marker() -> None:
    js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    html = (ROOT / "app/ui/dashboard.html").read_text(encoding="utf-8")
    assert "'← Все письма'" in js and "method: 'DELETE'" in js and "m.answered" in js
    assert 'id="adm-mail-in-list"' in html and 'id="adm-mail-out-list"' in html
