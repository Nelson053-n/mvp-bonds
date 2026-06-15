"""IndexNow client — instantly notify Yandex & Bing about new/changed URLs.

IndexNow is a simple protocol: POST a list of URLs plus an ownership key to a
single endpoint, and participating engines (Yandex, Bing, Seznam) pull and
(re)index them within minutes instead of waiting for the next crawl. Google does
not consume IndexNow, but Yandex is our primary market, so this is high-leverage.

Ownership is proven by hosting /<key>.txt (whose body is the key) — wired in
app/main.py. Disabled unless MVP_INDEXNOW_KEY is set.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_HOST = "bondai.ru"
_KEY_LOCATION = "https://bondai.ru/{key}.txt"
# Yandex endpoint accepts submissions and shares them across the IndexNow network.
_ENDPOINT = "https://yandex.com/indexnow"
_MAX_BATCH = 10000  # protocol cap per request


def enabled() -> bool:
    return bool(settings.indexnow_key)


async def submit(urls: list[str]) -> bool:
    """Submit up to _MAX_BATCH URLs to IndexNow. Returns True on a 2xx response.

    Best-effort: any failure is logged and swallowed — indexing hints must never
    break a request or a startup.
    """
    key = settings.indexnow_key
    if not key or not urls:
        return False
    payload = {
        "host": _HOST,
        "key": key,
        "keyLocation": _KEY_LOCATION.format(key=key),
        "urlList": urls[:_MAX_BATCH],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(_ENDPOINT, json=payload)
        ok = 200 <= resp.status_code < 300
        if ok:
            logger.info("IndexNow: submitted %d URLs (HTTP %d)", len(payload["urlList"]), resp.status_code)
        else:
            logger.warning("IndexNow: submit failed HTTP %d: %s", resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:  # noqa: BLE001 - never propagate
        logger.warning("IndexNow: submit error: %s", exc)
        return False
