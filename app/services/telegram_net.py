"""Подключение к api.telegram.org с перебором маршрутов.

С 09.10.2026 с прода перестал открываться 149.154.166.110:443 — единственный
адрес, который DNS (и наш резолвер, и 1.1.1.1, и 8.8.8.8) отдаёт для
api.telegram.org. Второй адрес Bot API, 149.154.167.220, при этом отвечал.
Бот, уведомления и коды восстановления пароля лежали целиком: за 10.10 —
722 ошибки подключения.

Подменяется только TCP-соединение: TLS по-прежнему идёт с SNI и проверкой
сертификата для api.telegram.org, поэтому чужой адрес в списке не пройдёт.
"""
from __future__ import annotations

import asyncio
import logging
import socket

import httpcore
import httpx

logger = logging.getLogger(__name__)

TG_HOST = "api.telegram.org"
# Адреса Bot API помимо DNS — на случай, когда DNS-адрес недоступен.
KNOWN_IPS = ("149.154.167.220", "149.154.166.110")
# Таймаут на один маршрут: с полным таймаутом клиента (до 45с у бота) до
# второго адреса дело бы просто не доходило.
_CONNECT_TIMEOUT = 5.0
_DNS_TIMEOUT = 3.0


async def _resolve() -> list[str]:
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                TG_HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM,
            ),
            _DNS_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("telegram: DNS %s не ответил: %s", TG_HOST, type(exc).__name__)
        return []
    return [info[4][0] for info in infos]


class _Routes:
    """Последний рабочий адрес процесса — его пробуем первым."""

    def __init__(self) -> None:
        self.preferred: str | None = None

    async def candidates(self) -> list[str]:
        ordered = [self.preferred] if self.preferred else []
        ordered += await _resolve()
        ordered += KNOWN_IPS
        return list(dict.fromkeys(ordered))


routes = _Routes()


class _FailoverBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend,
                 ips: list[str] | None = None) -> None:
        self._inner = inner
        # Явный список — для проверки одного маршрута: тогда preferred не трогаем.
        self._ips = ips

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        if host != TG_HOST:
            return await self._inner.connect_tcp(
                host, port, timeout, local_address, socket_options)
        ips = self._ips or await routes.candidates()
        per_ip = min(timeout or _CONNECT_TIMEOUT, _CONNECT_TIMEOUT)
        last_exc: Exception | None = None
        for ip in ips:
            try:
                stream = await self._inner.connect_tcp(
                    ip, port, per_ip, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
                continue
            if self._ips is None and ip != routes.preferred:
                if routes.preferred:
                    logger.warning(
                        "telegram: маршрут %s недоступен, переключились на %s",
                        routes.preferred, ip,
                    )
                routes.preferred = ip
            return stream
        assert last_exc is not None  # candidates() всегда содержит KNOWN_IPS
        raise last_exc

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def telegram_transport(ips: list[str] | None = None) -> httpx.AsyncHTTPTransport:
    """Транспорт httpx для запросов к Telegram: передавать в AsyncClient(transport=...)."""
    transport = httpx.AsyncHTTPTransport()
    # Публичного способа задать network_backend у httpx нет; httpx закреплён
    # в requirements (0.28.1), а test_telegram_net проверяет, что подмена
    # действительно работает — при смене версии тест покраснеет.
    pool = transport._pool
    pool._network_backend = _FailoverBackend(pool._network_backend, ips)
    return transport


async def probe_routes() -> dict[str, bool]:
    """HTTPS-запрос к Telegram через каждый маршрут по отдельности."""
    ips = await routes.candidates()

    async def one(ip: str) -> bool:
        try:
            async with httpx.AsyncClient(
                transport=telegram_transport([ip]), timeout=10,
            ) as client:
                await client.get(f"https://{TG_HOST}/")
            return True
        except httpx.HTTPError:
            return False

    results = await asyncio.gather(*(one(ip) for ip in ips))
    return dict(zip(ips, results))


def describe_change(prev: dict[str, bool] | None,
                    cur: dict[str, bool]) -> tuple[int, str] | None:
    """Что сообщить о смене доступности маршрутов; None — сообщать нечего.

    Сообщаем только о переходах, иначе при долгом сбое алерт уходил бы
    каждые 5 минут. Первый замер со всеми рабочими маршрутами — тишина.
    """
    down = sorted(ip for ip, ok in cur.items() if not ok)
    prev_down = sorted(ip for ip, ok in (prev or {}).items() if not ok)
    if down == prev_down:
        return None
    up = sorted(ip for ip, ok in cur.items() if ok)
    if not up:
        return logging.ERROR, "Telegram недоступен: не отвечает ни один маршрут (" + ", ".join(down) + ")"
    if down:
        return logging.WARNING, (
            "Telegram: недоступны " + ", ".join(down)
            + "; работаем через " + ", ".join(up)
        )
    return logging.INFO, "Telegram: все маршруты снова доступны (" + ", ".join(up) + ")"
