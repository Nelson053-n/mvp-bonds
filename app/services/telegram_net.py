"""Подключение к api.telegram.org с перебором маршрутов.

С 09.10.2026 с прода перестал открываться 149.154.166.110:443 — единственный
адрес, который DNS (и наш резолвер, и 1.1.1.1, и 8.8.8.8) отдаёт для
api.telegram.org. Второй адрес Bot API, 149.154.167.220, при этом отвечал.
Бот, уведомления и коды восстановления пароля лежали целиком: за 10.10 —
722 ошибки подключения.

Подменяется только TCP-соединение: TLS по-прежнему идёт с SNI и проверкой
сертификата для api.telegram.org, поэтому чужой адрес в списке не пройдёт.

Последний маршрут — SOCKS5-прокси (MVP_TG_SOCKS_PROXY) на случай, когда с
прода недоступны все адреса Telegram. На проде это туннель wg-google
(nanabanana) через Нидерланды; у него свой сторож с переключением нод.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import httpcore
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TG_HOST = "api.telegram.org"
# Адреса Bot API помимо DNS — на случай, когда DNS-адрес недоступен.
KNOWN_IPS = ("149.154.167.220", "149.154.166.110")
# Таймаут на один маршрут: с полным таймаутом клиента (до 45с у бота) до
# второго адреса дело бы просто не доходило.
_CONNECT_TIMEOUT = 5.0
_DNS_TIMEOUT = 3.0
_PROXY_PREFIX = "socks5://"
PROXY_FIRST_SECONDS = 300


_dns_failing = False


async def _resolve() -> list[str]:
    """Адреса из DNS; при сбое — пусто (выручат KNOWN_IPS).

    Резолв идёт на каждое подключение, поэтому о сбое пишем WARNING один раз,
    повторы — DEBUG, и INFO при восстановлении: иначе при упавшем резолвере
    десятки одинаковых строк в час, хотя связь при этом работает.
    """
    global _dns_failing
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                TG_HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM,
            ),
            _DNS_TIMEOUT,
        )
    except Exception as exc:
        log = logger.debug if _dns_failing else logger.warning
        log("telegram: DNS %s не ответил: %s", TG_HOST, type(exc).__name__)
        _dns_failing = True
        return []
    if _dns_failing:
        logger.info("telegram: DNS %s снова отвечает", TG_HOST)
        _dns_failing = False
    return [info[4][0] for info in infos]


class _Routes:
    """Последний рабочий прямой адрес процесса — его пробуем первым.

    Прокси (MVP_TG_SOCKS_PROXY) — последний маршрут. Если сработал только он,
    PROXY_FIRST_SECONDS пробуем его первым: иначе каждое подключение при полном
    сбое прямых адресов теряло бы по 5с на каждом из них.
    """

    def __init__(self) -> None:
        self.preferred: str | None = None
        self.proxy_until = 0.0

    async def candidates(self) -> list[str]:
        ordered = [self.preferred] if self.preferred else []
        ordered += await _resolve()
        ordered += KNOWN_IPS
        direct = list(dict.fromkeys(ordered))
        proxy = [_PROXY_PREFIX + settings.tg_socks_proxy] if settings.tg_socks_proxy else []
        if proxy and time.monotonic() < self.proxy_until:
            return proxy + direct
        return direct + proxy


routes = _Routes()

async def _read_exact(stream, n: int, timeout: float) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = await stream.read(n - len(buf), timeout)
        if not chunk:
            raise httpcore.ConnectError("SOCKS5: соединение закрыто")
        buf += chunk
    return buf


async def _socks5_connect(stream, host: str, port: int, timeout: float) -> None:
    """CONNECT через SOCKS5 без авторизации; имя резолвит сам прокси."""
    await stream.write(b"\x05\x01\x00", timeout)
    if await _read_exact(stream, 2, timeout) != b"\x05\x00":
        raise httpcore.ConnectError("SOCKS5: прокси не принял метод без авторизации")
    name = host.encode()
    await stream.write(
        b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"),
        timeout,
    )
    ver, rep, _, atyp = await _read_exact(stream, 4, timeout)
    if ver != 5 or rep != 0:
        raise httpcore.ConnectError(f"SOCKS5: отказ CONNECT, код {rep}")
    if atyp == 1:
        await _read_exact(stream, 4 + 2, timeout)
    elif atyp == 4:
        await _read_exact(stream, 16 + 2, timeout)
    else:
        (alen,) = await _read_exact(stream, 1, timeout)
        await _read_exact(stream, alen + 2, timeout)


class _FailoverBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend,
                 ips: list[str] | None = None) -> None:
        self._inner = inner
        # Явный список — для проверки одного маршрута: тогда состояние не трогаем.
        self._ips = ips

    async def _open(self, route: str, port: int, timeout: float,
                    local_address, socket_options):
        if not route.startswith(_PROXY_PREFIX):
            return await self._inner.connect_tcp(
                route, port, timeout, local_address, socket_options)
        phost, _, pport = route[len(_PROXY_PREFIX):].rpartition(":")
        stream = await self._inner.connect_tcp(
            phost, int(pport), timeout, local_address, socket_options)
        try:
            await _socks5_connect(stream, TG_HOST, port, timeout)
        except Exception as exc:
            await stream.aclose()
            if isinstance(exc, (httpcore.ConnectError, httpcore.ConnectTimeout)):
                raise
            raise httpcore.ConnectError(f"SOCKS5: {type(exc).__name__} {exc}") from exc
        return stream

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        if host != TG_HOST:
            return await self._inner.connect_tcp(
                host, port, timeout, local_address, socket_options)
        candidates = self._ips or await routes.candidates()
        per_route = min(timeout or _CONNECT_TIMEOUT, _CONNECT_TIMEOUT)
        last_exc: Exception | None = None
        for route in candidates:
            try:
                stream = await self._open(
                    route, port, per_route, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
                continue
            if self._ips is None:
                self._remember(route)
            return stream
        assert last_exc is not None  # candidates() всегда содержит KNOWN_IPS
        raise last_exc

    @staticmethod
    def _remember(route: str) -> None:
        if route.startswith(_PROXY_PREFIX):
            if time.monotonic() >= routes.proxy_until:
                logger.warning("telegram: прямые адреса недоступны, работаем через %s", route)
            routes.proxy_until = time.monotonic() + PROXY_FIRST_SECONDS
            return
        if route != routes.preferred:
            if routes.preferred:
                logger.warning(
                    "telegram: маршрут %s недоступен, переключились на %s",
                    routes.preferred, route,
                )
            routes.preferred = route

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


def confirm(confirmed: dict[str, bool], prev_raw: dict[str, bool] | None,
            cur_raw: dict[str, bool]) -> dict[str, bool]:
    """Подтверждённое состояние: меняется, только если два замера подряд совпали.

    149.154.166.110 мигал — пропадал на один замер каждые 30–60 минут, и
    каждый раз уходило два алерта (12 за 2.3 часа 10–11.10). Новый адрес без
    подтверждённой истории считаем рабочим, пока два замера не скажут иное.
    """
    return {
        ip: ok if prev_raw is not None and prev_raw.get(ip) == ok
        else confirmed.get(ip, True)
        for ip, ok in cur_raw.items()
    }


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


async def watch_routes() -> None:
    """Раз в 5 минут проверяет каждый маршрут до api.telegram.org.

    Сообщает только о смене подтверждённого состояния (два замера подряд).
    Алерт уходит в Telegram, пока жив хоть один маршрут; при полном сбое —
    письмом на MVP_ALERT_EMAIL. Состояние хранится в app_settings: иначе после
    каждого деплоя тот же сбой объявлялся заново.
    """
    # Лениво: notification_service сам импортирует этот модуль.
    from app.services.notification_service import notification_service
    from app.services.storage_service import storage_service

    try:
        confirmed = json.loads(storage_service.get_setting("tg_routes_state", "{}"))
    except Exception:
        logger.exception("telegram_routes: битый tg_routes_state, начинаем с нуля")
        confirmed = {}
    prev_raw = None
    while True:
        try:
            cur = await probe_routes()
            new = confirm(confirmed, prev_raw, cur)
            prev_raw = cur
            change = describe_change(confirmed, new)
            if new != confirmed:
                storage_service.set_setting("tg_routes_state", json.dumps(new))
                confirmed = new
            if change:
                level, text = change
                logger.log(level, "telegram_routes: %s", text)
                s = storage_service.get_all_settings()
                if level == logging.ERROR:
                    await notification_service.send_alert_email(
                        "bondai.ru: Telegram недоступен", text)
                elif s.get("tg_bot_token") and s.get("tg_chat_id"):
                    await notification_service.send_telegram(
                        s["tg_bot_token"], s["tg_chat_id"], text)
        except Exception:
            logger.exception("telegram_routes: probe failed")
        await asyncio.sleep(300)
