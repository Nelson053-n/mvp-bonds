"""CBR (Bank of Russia) key rate service."""

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_FALLBACK_KEY_RATE = 16.0
_SUCCESS_TTL_SECONDS = 24 * 3600
_FAILURE_TTL_SECONDS = 10 * 60   # cache failures briefly so requests don't keep hitting the network

# CBR removed scripts/xml_keyrate.asp (returns 302 → /Error/404). The HTML page
# at /key-indicators/ is the only stable public source today; we scrape the
# "Ключевая ставка" block and pull the percentage near it.
_KEY_INDICATORS_URL = "https://www.cbr.ru/key-indicators/"
_KEY_RATE_RE = re.compile(
    r"Ключевая\s*ставка.*?<div\s+class=\"value\">\s*([0-9]{1,2},[0-9]{2})\s*%",
    re.DOTALL,
)


class CBRService:
    """Fetch Bank of Russia key rate. Caches both success and failure to avoid
    proxying every request through the (often slow / broken) CBR site."""

    def __init__(self) -> None:
        # (rate, ts, is_fallback): when is_fallback is True we re-fetch after
        # _FAILURE_TTL_SECONDS; on real success we hold for _SUCCESS_TTL_SECONDS.
        self._cache: tuple[float, float, bool] | None = None

    async def get_key_rate(self) -> float:
        now = time.time()
        if self._cache is not None:
            rate, ts, is_fallback = self._cache
            ttl = _FAILURE_TTL_SECONDS if is_fallback else _SUCCESS_TTL_SECONDS
            if now - ts < ttl:
                return rate

        rate = await self._fetch_key_rate()
        if rate is None:
            # Negative cache: stop hammering CBR for the next _FAILURE_TTL_SECONDS.
            stale = self._cache[0] if self._cache else _FALLBACK_KEY_RATE
            self._cache = (stale, now, True)
            logger.warning("CBR key rate unavailable, using %.2f for %ds", stale, _FAILURE_TTL_SECONDS)
            return stale
        self._cache = (rate, now, False)
        logger.info("CBR key rate updated: %.2f%%", rate)
        return rate

    async def _fetch_key_rate(self) -> float | None:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                resp = await client.get(_KEY_INDICATORS_URL, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to fetch CBR key-indicators page: %s", exc)
            return None
        match = _KEY_RATE_RE.search(resp.text)
        if not match:
            logger.warning("CBR key-indicators page parsed but rate pattern not found")
            return None
        try:
            return float(match.group(1).replace(",", "."))
        except ValueError:
            return None


cbr_service = CBRService()
