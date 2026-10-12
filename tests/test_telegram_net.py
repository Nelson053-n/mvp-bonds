"""Перебор маршрутов до api.telegram.org.

С 09.10.2026 DNS-адрес 149.154.166.110 с прода недоступен, второй адрес
Bot API 149.154.167.220 отвечает. Без перебора лежали бот, уведомления и
коды восстановления пароля.
"""
import asyncio
import logging

import httpcore
import httpx
import pytest

from app.services import telegram_net

_REAL_RESOLVE = telegram_net._resolve  # autouse-фикстура ниже подменяет _resolve
from app.services.telegram_net import (
    TG_HOST, _FailoverBackend, describe_change, telegram_transport,
)


class _FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self, dead: set[str]) -> None:
        self.dead = dead
        self.calls: list[tuple[str, float | None]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        self.calls.append((host, timeout))
        if host in self.dead:
            raise httpcore.ConnectTimeout("")
        return f"stream:{host}"

    async def sleep(self, seconds):
        pass


@pytest.fixture(autouse=True)
def _reset_routes(monkeypatch):
    telegram_net.routes.preferred = None
    telegram_net.routes.proxy_until = 0.0
    monkeypatch.setattr(telegram_net.settings, "tg_socks_proxy", "")

    async def fake_resolve():
        return ["149.154.166.110"]
    monkeypatch.setattr(telegram_net, "_resolve", fake_resolve)
    yield
    telegram_net.routes.preferred = None


async def test_falls_over_to_next_ip_and_remembers_it() -> None:
    inner = _FakeInner(dead={"149.154.166.110"})
    backend = _FailoverBackend(inner)
    stream = await backend.connect_tcp(TG_HOST, 443, timeout=45)
    assert stream == "stream:149.154.167.220"
    assert [h for h, _ in inner.calls] == ["149.154.166.110", "149.154.167.220"]
    # Таймаут на маршрут урезан, иначе до второго адреса не дойти.
    assert all(t == 5.0 for _, t in inner.calls)
    assert telegram_net.routes.preferred == "149.154.167.220"

    inner.calls.clear()
    await backend.connect_tcp(TG_HOST, 443, timeout=45)
    assert [h for h, _ in inner.calls] == ["149.154.167.220"]


async def test_all_dead_raises_connect_error() -> None:
    backend = _FailoverBackend(_FakeInner(dead={"149.154.166.110", "149.154.167.220"}))
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp(TG_HOST, 443, timeout=10)


async def test_other_hosts_untouched() -> None:
    inner = _FakeInner(dead=set())
    stream = await _FailoverBackend(inner).connect_tcp("iss.moex.com", 443, timeout=10)
    assert stream == "stream:iss.moex.com"
    assert inner.calls == [("iss.moex.com", 10)]


async def test_explicit_ips_do_not_change_preferred() -> None:
    backend = _FailoverBackend(_FakeInner(dead=set()), ips=["149.154.166.110"])
    await backend.connect_tcp(TG_HOST, 443)
    assert telegram_net.routes.preferred is None


async def test_real_httpx_request_goes_through_failover() -> None:
    """Сквозь настоящий httpx: подмена network_backend действительно работает.

    Сервер слушает только 127.0.0.1; на 127.0.0.2 соединение отклоняется,
    и запрос должен уйти по второму маршруту.
    """
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            transport=telegram_transport(["127.0.0.2", "127.0.0.1"]), timeout=5,
        ) as client:
            # 127.0.0.2 тоже loopback; на нём никто не слушает этот порт.
            r = await client.get(f"http://{TG_HOST}:{port}/")
        assert r.text == "ok"
    finally:
        server.close()
        await server.wait_closed()


class TestDescribeChange:
    def test_first_probe_all_up_is_silent(self) -> None:
        assert describe_change(None, {"a": True, "b": True}) is None

    def test_route_down_warns_with_working_route(self) -> None:
        level, text = describe_change({"a": True, "b": True}, {"a": False, "b": True})
        assert level == logging.WARNING
        assert "a" in text and "через b" in text

    def test_same_state_is_silent(self) -> None:
        state = {"a": False, "b": True}
        assert describe_change(state, dict(state)) is None

    def test_all_down_is_error(self) -> None:
        level, _ = describe_change({"a": False, "b": True}, {"a": False, "b": False})
        assert level == logging.ERROR

    def test_recovery_is_info(self) -> None:
        level, _ = describe_change({"a": False, "b": True}, {"a": True, "b": True})
        assert level == logging.INFO


class TestConfirm:
    def test_needs_two_equal_probes(self) -> None:
        from app.services.telegram_net import confirm
        assert confirm({"a": True}, {"a": True}, {"a": False}) == {"a": True}
        assert confirm({"a": True}, {"a": False}, {"a": False}) == {"a": False}

    def test_first_probe_keeps_confirmed(self) -> None:
        from app.services.telegram_net import confirm
        assert confirm({"a": False}, None, {"a": True}) == {"a": False}

    def test_unknown_ip_assumed_up(self) -> None:
        from app.services.telegram_net import confirm
        assert confirm({}, None, {"b": False}) == {"b": True}

    def test_dropped_ip_forgotten(self) -> None:
        """Адрес пропал из кандидатов — не висит «недоступным» навсегда."""
        from app.services.telegram_net import confirm
        assert confirm({"a": False, "b": True}, {"b": True}, {"b": True}) == {"b": True}


async def _http_server():
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _socks_server(target_port: int, seen: list, reply: int = 0):
    """Минимальный SOCKS5: любой CONNECT ведёт на локальный HTTP-сервер."""
    async def handle(reader, writer):
        assert await reader.readexactly(3) == b"\x05\x01\x00"
        writer.write(b"\x05\x00")
        ver, cmd, _, atyp = await reader.readexactly(4)
        name = await reader.readexactly((await reader.readexactly(1))[0])
        port = int.from_bytes(await reader.readexactly(2), "big")
        seen.append((name.decode(), port))
        if reply:
            writer.write(bytes([5, reply, 0, 1]) + b"\0" * 6)
            await writer.drain()
            writer.close()
            return
        writer.write(b"\x05\x00\x00\x01" + b"\x7f\x00\x00\x01" + port.to_bytes(2, "big"))
        up_r, up_w = await asyncio.open_connection("127.0.0.1", target_port)

        async def pipe(r, w):
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            finally:
                w.close()
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_falls_back_to_socks_proxy_end_to_end() -> None:
    """Прямой адрес отказывает — запрос уходит через SOCKS5, имя резолвит прокси."""
    http = await _http_server()
    hport = http.sockets[0].getsockname()[1]
    seen: list = []
    socks = await _socks_server(hport, seen)
    sport = socks.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            transport=telegram_transport(["127.0.0.2", f"socks5://127.0.0.1:{sport}"]),
            timeout=5,
        ) as client:
            r = await client.get(f"http://{TG_HOST}:{hport}/")
        assert r.text == "ok"
        assert seen == [(TG_HOST, hport)]
    finally:
        for srv in (http, socks):
            srv.close()
            await srv.wait_closed()


async def test_socks_refusal_is_connect_error() -> None:
    seen: list = []
    socks = await _socks_server(1, seen, reply=5)
    sport = socks.sockets[0].getsockname()[1]
    try:
        backend = _FailoverBackend(httpcore.AnyIOBackend(), ips=[f"socks5://127.0.0.1:{sport}"])
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp(TG_HOST, 443, timeout=5)
    finally:
        socks.close()
        await socks.wait_closed()


async def test_proxy_last_then_first_after_use(monkeypatch) -> None:
    monkeypatch.setattr(telegram_net.settings, "tg_socks_proxy", "127.0.0.1:1082")
    cands = await telegram_net.routes.candidates()
    assert cands[-1] == "socks5://127.0.0.1:1082"
    _FailoverBackend._remember("socks5://127.0.0.1:1082")
    cands = await telegram_net.routes.candidates()
    assert cands[0] == "socks5://127.0.0.1:1082"
    # Прокси не становится «предпочтительным прямым адресом».
    assert telegram_net.routes.preferred is None


async def test_no_proxy_configured() -> None:
    assert not any(c.startswith("socks5://") for c in await telegram_net.routes.candidates())


async def test_dns_failure_warns_once(monkeypatch, caplog) -> None:
    """Упавший DNS: один WARNING на сбой, а не на каждое подключение."""
    import socket as _socket
    monkeypatch.setattr(telegram_net, "_dns_failing", False)

    async def boom(*a, **k):
        raise _socket.gaierror("down")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", boom)
    with caplog.at_level(logging.DEBUG, logger="app.services.telegram_net"):
        for _ in range(5):
            assert await _REAL_RESOLVE() == []
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
