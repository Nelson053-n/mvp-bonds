"""CBR (Bank of Russia) key rate service."""

import logging
import time
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

_FALLBACK_KEY_RATE = 16.0
_TTL_SECONDS = 24 * 3600


class CBRService:
    """Fetch Bank of Russia key rate. Cache 24h, fallback to constant."""

    def __init__(self) -> None:
        self._cache: tuple[float, float] | None = None  # (rate, ts)

    async def get_key_rate(self) -> float:
        now = time.time()
        if self._cache and (now - self._cache[1]) < _TTL_SECONDS:
            return self._cache[0]
        rate = await self._fetch_key_rate()
        if rate is None:
            if self._cache:
                logger.warning("Using stale CBR key rate: %.2f", self._cache[0])
                return self._cache[0]
            logger.warning("CBR key rate unavailable, fallback %.2f", _FALLBACK_KEY_RATE)
            return _FALLBACK_KEY_RATE
        self._cache = (rate, now)
        logger.info("CBR key rate updated: %.2f%%", rate)
        return rate

    async def _fetch_key_rate(self) -> float | None:
        # KeyRate XML feed; DateFrom = today is enough — server returns latest.
        from datetime import datetime
        today = datetime.utcnow().strftime("%d/%m/%Y")
        url = (
            "https://www.cbr.ru/scripts/xml_keyrate.asp"
            f"?DateFrom={today}&DateTo={today}"
        )
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
            records = root.findall(".//Record")
            if not records:
                # No data for today (e.g., weekend) — try last 14 days.
                from datetime import timedelta
                week_ago = (datetime.utcnow() - timedelta(days=14)).strftime("%d/%m/%Y")
                url2 = (
                    "https://www.cbr.ru/scripts/xml_keyrate.asp"
                    f"?DateFrom={week_ago}&DateTo={today}"
                )
                async with httpx.AsyncClient(timeout=8) as client:
                    resp = await client.get(url2, headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                root = ET.fromstring(resp.text)
                records = root.findall(".//Record")
            if not records:
                return None
            last = records[-1]
            rate_el = last.find("Rate")
            if rate_el is None or rate_el.text is None:
                return None
            return float(rate_el.text.replace(",", "."))
        except Exception as exc:
            logger.warning("Failed to fetch CBR key rate: %s", exc)
            return None


cbr_service = CBRService()
