import asyncio
from datetime import date
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings
from app.models import BondSnapshot, StockSnapshot
from app.exceptions import PriceNotFoundError, DataFetchError, RatingNotFoundError

logger = logging.getLogger(__name__)

# Retries for transient MOEX failures. Kept short: the cache loop runs every
# 300s and refreshes portfolios concurrently, so a long backoff would stack up.
_RETRY_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.5)  # pauses between attempts 1→2 and 2→3
_RETRY_STATUS = frozenset({500, 502, 503, 504})


class SourceStats:
    """Statistics for a single external data source."""
    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.enabled: bool = True
        self.requests: int = 0
        self.hits: int = 0       # got useful data
        self.errors: int = 0     # HTTP / network error
        self.blocked: int = 0    # 403/429 bot-block responses
        self.last_attempt: float = 0.0
        self.last_success: float = 0.0
        self.last_error_code: int | None = None
        self.last_error_msg: str = ""

    def record_hit(self) -> None:
        self.requests += 1
        self.hits += 1
        self.last_attempt = time.time()
        self.last_success = time.time()

    def record_miss(self) -> None:
        """Got valid response but no useful data (e.g. rating not found in page)."""
        self.requests += 1
        self.last_attempt = time.time()

    def record_error(self, status_code: int | None = None, msg: str = "") -> None:
        self.requests += 1
        self.errors += 1
        self.last_attempt = time.time()
        if status_code in (403, 429, 503):
            self.blocked += 1
        self.last_error_code = status_code
        self.last_error_msg = msg[:120]

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "requests": self.requests,
            "hits": self.hits,
            "errors": self.errors,
            "blocked": self.blocked,
            "hit_rate": round(self.hits / self.requests * 100, 1) if self.requests else None,
            "last_attempt_ago": round(now - self.last_attempt) if self.last_attempt else None,
            "last_success_ago": round(now - self.last_success) if self.last_success else None,
            "last_error_code": self.last_error_code,
            "last_error_msg": self.last_error_msg,
        }


class _RatingCache:
    """In-memory rating cache with differentiated TTL.

    Drop-in for the old plain dict (same []/in/pop/clear/len interface) but
    entries expire, so a once-fetched rating is re-checked instead of being
    served stale forever. A successful rating is cached for OK_TTL; a None
    (source error / not-found) only for MISS_TTL so a transient SmartLab
    timeout can't pin a bond to "no rating" until the next restart.
    """
    OK_TTL = 6 * 3600     # 6h for a real rating
    MISS_TTL = 900        # 15min for an honest None (rating genuinely absent)
    ERROR_TTL = 60        # 1min for a None from a transient source error

    def __init__(self) -> None:
        self._data: dict[str, tuple[object, float]] = {}
        self._errored: set[str] = set()

    def set_error(self, key: str) -> None:
        """Cache None from a source error with a short ERROR_TTL."""
        self._data[key] = (None, time.time())
        self._errored.add(key)

    def _fresh(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return False
        value, ts = entry
        if value:
            ttl = self.OK_TTL
        elif key in self._errored:
            ttl = self.ERROR_TTL
        else:
            ttl = self.MISS_TTL
        if (time.time() - ts) >= ttl:
            self._data.pop(key, None)
            self._errored.discard(key)
            return False
        return True

    def __contains__(self, key: str) -> bool:
        return self._fresh(key)

    def __getitem__(self, key: str):
        return self._data[key][0]

    MAX_ENTRIES = 4000

    def __setitem__(self, key: str, value) -> None:
        # Протухшие записи удаляются только при чтении того же ключа, поэтому
        # рейтинги бумаг, запрошенных однажды (каталог /bond — ~2700 штук),
        # оставались в памяти навсегда. При переполнении подчищаем истёкшие,
        # а если не помогло — вытесняем самые старые.
        if len(self._data) >= self.MAX_ENTRIES:
            for k in [k for k in list(self._data) if not self._fresh(k)]:
                self._data.pop(k, None)
                self._errored.discard(k)
            if len(self._data) >= self.MAX_ENTRIES:
                oldest = sorted(self._data, key=lambda k: self._data[k][1])
                for k in oldest[: self.MAX_ENTRIES // 10]:
                    self._data.pop(k, None)
                    self._errored.discard(k)
        self._data[key] = (value, time.time())
        self._errored.discard(key)

    def pop(self, key: str, default=None):
        self._errored.discard(key)
        entry = self._data.pop(key, None)
        return entry[0] if entry is not None else default

    def clear(self) -> None:
        self._data.clear()
        self._errored.clear()

    def __len__(self) -> int:
        return len(self._data)


class MOEXService:
    RATING_PATTERN = re.compile(
        r"^(ru(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|"
        r"B[+-]?|CCC|CC|C|D)(?:\(EXP\))?)$",
        re.IGNORECASE,
    )
    BARE_RATING_PATTERN = re.compile(
        r"^(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)$",
        re.IGNORECASE,
    )

    FX_RATE_TTL = 3600   # 1 hour cache for FX rates
    SNAPSHOT_TTL = 60    # 60 sec cache for bond/stock snapshots
    # Потолок снапшот-кэшей. Без вытеснения TTL проверялся только при чтении,
    # а протухшие записи не удалялись никогда: каталог /bond прогревает ~2700
    # бумаг, и они оседали в памяти воркера-лидера навсегда. На проде это дало
    # 701 МБ RSS при MemoryHigh=750 МБ и 3726 срабатываний throttling.
    SNAPSHOT_CACHE_MAX = 1200

    def __init__(self) -> None:
        self._credit_rating_cache = _RatingCache()
        self._is_qual_cache: dict[str, tuple[bool, bool]] = {}  # secid -> (is_qual, is_traded)
        self._fx_rate_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate, timestamp)
        self._bond_snapshot_cache: dict[str, tuple[Any, float]] = {}  # secid -> (snapshot, ts)
        self._stock_snapshot_cache: dict[str, tuple[Any, float]] = {}  # secid -> (snapshot, ts)
        self.sources: dict[str, SourceStats] = {
            "moex_price": SourceStats("moex_price", "MOEX ISS (цены)"),
            "moex_rating": SourceStats("moex_rating", "MOEX ISS (рейтинг)"),
            "smartlab": SourceStats("smartlab", "Smart-Lab (рейтинг)"),
            "moex_fx": SourceStats("moex_fx", "MOEX ISS (валюты)"),
        }

    def _snapshot_cache_put(
        self, cache: dict[str, tuple[Any, float]], secid: str, snapshot: Any
    ) -> None:
        """Положить снапшот в кэш, вытеснив лишнее при достижении потолка.

        Сначала выбрасываем протухшие по TTL — их обычно большинство, ведь
        SNAPSHOT_TTL всего 60с. Если и после этого места нет (редкий всплеск),
        добиваем самыми старыми записями. Та же схема, что у _cache_put в
        bond_pages.py.
        """
        if len(cache) >= self.SNAPSHOT_CACHE_MAX:
            now = time.time()
            for k in [k for k, v in cache.items() if now - v[1] > self.SNAPSHOT_TTL]:
                cache.pop(k, None)
            if len(cache) >= self.SNAPSHOT_CACHE_MAX:
                oldest = sorted(cache, key=lambda k: cache[k][1])
                for k in oldest[: max(1, self.SNAPSHOT_CACHE_MAX // 10)]:
                    cache.pop(k, None)
        cache[secid] = (snapshot, time.time())

    def get_sources_status(self) -> list[dict]:
        return [s.to_dict() for s in self.sources.values()]

    def set_source_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self.sources:
            return False
        self.sources[name].enabled = enabled
        return True

    async def refresh_rating(self, secid: str) -> str | None:
        """Force re-fetch rating, bypassing in-memory cache."""
        self._credit_rating_cache.pop(secid, None)
        self._credit_rating_cache.pop(f"smartlab:{secid}", None)
        rating = await self._get_smartlab_credit_rating(secid)
        if rating is None:
            rating = await self._get_credit_rating(secid)
        return rating

    async def refresh_rating_with_sources(self, secid: str) -> dict[str, str | None]:
        """Force re-fetch rating from both SmartLab and MOEX separately.

        Returns {"smartlab": rating_or_None, "moex": rating_or_None, "best": best_rating}.
        """
        self._credit_rating_cache.pop(secid, None)
        self._credit_rating_cache.pop(f"smartlab:{secid}", None)
        sl_rating, moex_rating = await asyncio.gather(
            self._get_smartlab_credit_rating(secid),
            self._get_credit_rating(secid),
            return_exceptions=False,
        )
        best = sl_rating or moex_rating
        return {"smartlab": sl_rating, "moex": moex_rating, "best": best}

    async def _get_fx_rate(self, currency: str) -> float | None:
        """Get FX rate for currency to RUB. Returns 1.0 for SUR/RUB."""
        if currency in ("SUR", "RUB"):
            return 1.0

        now = time.time()
        cached = self._fx_rate_cache.get(currency)
        if cached and (now - cached[1]) < self.FX_RATE_TTL:
            return cached[0]

        src = self.sources["moex_fx"]
        if not src.enabled:
            return cached[0] if cached else None

        url = (
            f"{settings.moex_base_url}/statistics/engines/futures/markets/"
            f"indicativerates/securities/{currency}/RUB.json"
            f"?iss.meta=off&iss.only=securities.current"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            current = data.get("securities.current", {})
            cols = current.get("columns", [])
            rows = current.get("data", [])
            if rows:
                row = dict(zip(cols, rows[-1]))
                rate = row.get("rate")
                if rate is not None:
                    rate = float(rate)
                    self._fx_rate_cache[currency] = (rate, now)
                    src.record_hit()
                    logger.info("FX rate %s/RUB = %.4f", currency, rate)
                    return rate
            src.record_miss()
            logger.warning("FX rate not found for %s/RUB", currency)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            src.record_error(status, str(exc))
            logger.warning("Failed to fetch FX rate %s/RUB: %s", currency, exc)

        # Fallback to stale cache
        if cached:
            logger.warning("Using stale FX rate for %s/RUB: %.4f", currency, cached[0])
            return cached[0]
        return None

    async def get_index_value(self, index_id: str = "RGBI") -> float | None:
        """Get current value of MOEX index (e.g., RGBI for OFZ index)."""
        url = (
            f"{settings.moex_base_url}/engines/stock/markets/index/"
            f"securities/{index_id}.json?iss.meta=off&iss.only=marketdata"
        )
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            md = data.get("marketdata", {})
            cols = md.get("columns", [])
            rows = md.get("data", [])
            if not rows:
                return None
            row = dict(zip(cols, rows[0]))
            val = row.get("CURRENTVALUE") or row.get("LASTVALUE")
            return float(val) if val is not None else None
        except Exception as exc:
            logger.warning("Failed to fetch index %s: %s", index_id, exc)
            return None

    async def get_index_history(self, index_id: str = "RGBI", days: int = 400) -> list[dict]:
        """Get historical daily close values of an index. Returns [{date, value}, ...]."""
        from datetime import date, timedelta
        date_from = (date.today() - timedelta(days=days)).isoformat()
        url = (
            f"{settings.moex_base_url}/history/engines/stock/markets/index/"
            f"securities/{index_id}.json?from={date_from}&iss.meta=off"
            f"&iss.only=history&history.columns=TRADEDATE,CLOSE"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            hist = data.get("history", {})
            cols = hist.get("columns", [])
            rows = hist.get("data", [])
            result = []
            for r in rows:
                d = dict(zip(cols, r))
                val = d.get("CLOSE")
                tdate = d.get("TRADEDATE")
                if val is not None and tdate:
                    result.append({"date": tdate, "value": float(val)})
            return result
        except Exception as exc:
            logger.warning("Failed to fetch index history %s: %s", index_id, exc)
            return []

    async def get_stock_snapshot(self, ticker: str) -> StockSnapshot:
        secid = ticker.upper().strip()
        cached = self._stock_snapshot_cache.get(secid)
        if cached and (time.time() - cached[1]) < self.SNAPSHOT_TTL:
            return cached[0]

        url = (
            f"{settings.moex_base_url}/engines/stock/markets/"
            f"shares/boards/TQBR/securities/{secid}.json"
        )
        data = await self._fetch(url)

        sec_row = self._get_first_row(data.get("securities", {}))
        md_row = self._get_first_row(data.get("marketdata", {}))

        name = sec_row.get("SHORTNAME") or sec_row.get("SECNAME") or secid
        # Before the session opens MOEX clears LAST and leaves LCLOSE empty, so
        # both intraday fields are None between ~03:00 and 07:00 UTC. Falling
        # back to the previous close keeps the portfolio priced overnight —
        # same chain the bond branch already uses.
        current_price = (
            md_row.get("LAST")
            or md_row.get("LCLOSE")
            or sec_row.get("PREVPRICE")
            or sec_row.get("PREVLEGALCLOSEPRICE")
            or sec_row.get("PREVWAPRICE")
        )
        if not current_price:
            # Пустой securities = бумаги на торговом эндпоинте нет вовсе:
            # иностранная (BIG — акция американской Big Lots из синка Т-Банка),
            # делистингованная или снятая с торгов. Цены у неё не будет никогда,
            # поэтому это норма, а не сбой — тот же критерий, что у облигаций.
            # Сетевые ошибки и 5xx сюда не доходят: их ловит _fetch.
            if not data.get("securities", {}).get("data"):
                logger.debug("Нет цены акции %s: не торгуется на MOEX", secid)
            else:
                logger.error("Не удалось получить цену акции %s", secid)
            raise PriceNotFoundError(secid, "акция")
        company_rating = await self._get_credit_rating(secid)

        # Previous trading session close, for the "day P&L" mode.
        #   • current = LAST (intraday)        → prev = LCLOSE (yesterday's close)
        #   • current = LCLOSE (today's close) → prev = yesterday via PREV* fields
        #   • current = PREV* (session not open) → prev = current ⇒ zero day change
        #   • neither / no prev source         → prev = current ⇒ zero day change
        prev_close = None
        has_last = md_row.get("LAST") is not None
        has_intraday = has_last or md_row.get("LCLOSE") is not None
        if has_last and md_row.get("LCLOSE") is not None:
            try:
                prev_close = float(md_row.get("LCLOSE"))
            except (TypeError, ValueError):
                prev_close = None
        # Only look for a yesterday-close when the current price is an intraday
        # one. If current_price ITSELF came from a PREV* field (session not open
        # yet), comparing it against another PREV* field would invent a day
        # change out of two different flavours of the same close.
        if prev_close is None and has_intraday:
            for cand in (sec_row.get("PREVLEGALCLOSEPRICE"), sec_row.get("PREVPRICE")):
                if cand is not None:
                    try:
                        prev_close = float(cand)
                        break
                    except (TypeError, ValueError):
                        continue
        if prev_close is None:
            prev_close = float(current_price)

        snapshot = StockSnapshot(
            ticker=secid,
            name=str(name),
            current_price=float(current_price),
            prev_close_price=prev_close,
            dividend_yield=None,
            company_rating=company_rating,
            rating_source="moex" if company_rating else None,
        )
        self._snapshot_cache_put(self._stock_snapshot_cache, secid, snapshot)
        return snapshot

    def invalidate_snapshot_cache(self, secid: str) -> None:
        """Remove a ticker from snapshot caches (call after manual data change)."""
        self._bond_snapshot_cache.pop(secid.upper(), None)
        self._stock_snapshot_cache.pop(secid.upper(), None)

    async def get_last_known_coupon(self, secid: str) -> dict | None:
        """Fetch the last known coupon from bondization for floaters.

        Returns {"value": float, "period_days": int} or None if unavailable.
        Only called when MOEX reports COUPONVALUE=0 (floater / not yet announced).
        """
        url = (
            f"{settings.moex_base_url}/securities/{secid}/bondization.json"
            "?iss.meta=off&iss.only=coupons&limit=50"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug("bondization fetch failed for %s: %s", secid, exc)
            return None

        cols = data.get("coupons", {}).get("columns", [])
        rows = data.get("coupons", {}).get("data", [])
        if not cols or not rows:
            return None

        # Find last row with a non-zero value
        last_value: float | None = None
        last_start: str | None = None
        last_end: str | None = None
        for row in rows:
            r = dict(zip(cols, row))
            v = r.get("value")
            if v is not None and float(v) > 0:
                last_value = float(v)
                last_start = r.get("startdate")
                last_end = r.get("coupondate")

        if last_value is None:
            return None

        # Calculate actual period length in days
        period_days: int | None = None
        if last_start and last_end:
            try:
                from datetime import date as _date
                d1 = _date.fromisoformat(last_start)
                d2 = _date.fromisoformat(last_end)
                period_days = (d2 - d1).days
            except Exception:
                pass

        return {"value": last_value, "period_days": period_days}

    async def get_bond_snapshot(self, ticker: str) -> BondSnapshot:
        secid = ticker.upper().strip()
        cached = self._bond_snapshot_cache.get(secid)
        if cached and (time.time() - cached[1]) < self.SNAPSHOT_TTL:
            return cached[0]

        url = (
            f"{settings.moex_base_url}/engines/stock/markets/"
            f"bonds/securities/{secid}.json"
        )
        data = await self._fetch(url)

        sec_row = self._get_security_row(data.get("securities", {}))
        md_row = self._get_row_with_price(data.get("marketdata", {}))

        name = sec_row.get("SHORTNAME") or sec_row.get("SECNAME") or secid
        clean_price_percent = (
            md_row.get("LAST")
            or md_row.get("LCLOSE")
            or sec_row.get("PREVPRICE")
            or sec_row.get("PREVWAPRICE")
            or sec_row.get("PREVLEGALCLOSEPRICE")
        )
        if clean_price_percent is None:
            # Пустой securities = бумаги на торговом эндпоинте нет вовсе:
            # погашена, внебиржевая или снята с торгов. Цены у неё не будет
            # никогда, поэтому это норма, а не сбой — в лог на уровне DEBUG.
            # Сетевые ошибки и 5xx сюда не доходят: их ловит _fetch.
            if not data.get("securities", {}).get("data"):
                logger.debug("Нет цены облигации %s: не торгуется на MOEX", secid)
            else:
                logger.error("Не удалось получить цену облигации %s", secid)
            raise PriceNotFoundError(secid, "облигация")

        # Previous trading session close % of face, for the "day P&L" mode.
        # The previous-close source depends on WHERE the current price came from,
        # so we never compare a price with itself:
        #   • current = LAST (intraday)        → prev = yesterday's close (PREV* fields)
        #   • current = LCLOSE (today's close) → prev = yesterday's close (PREV* fields)
        #   • current = PREV* (no trades today)→ prev = current ⇒ day change is 0
        prev_close_percent: float | None = None
        has_last = md_row.get("LAST") is not None
        has_lclose = md_row.get("LCLOSE") is not None
        if has_last or has_lclose:
            # Yesterday's close candidates. LCLOSE (today's close) is a valid
            # "yesterday" reference only while current = LAST (intraday); when
            # current already IS LCLOSE we must skip it to avoid self-comparison.
            cands = [
                sec_row.get("PREVLEGALCLOSEPRICE"),
                sec_row.get("PREVPRICE"),
                sec_row.get("PREVWAPRICE"),
            ]
            if has_last:
                cands.append(md_row.get("LCLOSE"))
            for cand in cands:
                if cand is not None:
                    try:
                        prev_close_percent = float(cand)
                        break
                    except (TypeError, ValueError):
                        continue
        if prev_close_percent is None:
            # No trades today (current price IS a previous close) → zero day change.
            prev_close_percent = float(clean_price_percent)

        nominal = sec_row.get("FACEVALUE")
        coupon = sec_row.get("COUPONVALUE")
        coupon_period = sec_row.get("COUPONPERIOD")
        coupon_rate = sec_row.get("COUPONPERCENT")  # Ставка купона в %
        bond_type = sec_row.get("BONDTYPE", "")  # "Флоатер" for floaters

        # Floater detection by bond type — independent of whether MOEX
        # currently reports a coupon (it may already know the announced one).
        is_floater = (
            "флоатер" in (bond_type or "").lower()
            or "float" in (bond_type or "").lower()
        )
        # When MOEX has no coupon yet (COUPONVALUE=0) fetch last known from bondization
        coupon_unknown = (coupon is None or float(coupon) == 0) and (
            coupon_rate is None or float(coupon_rate) == 0
        )
        if is_floater and coupon_unknown:
            last_coupon = await self.get_last_known_coupon(secid)
            if last_coupon:
                coupon = last_coupon["value"]
                # Recalculate coupon_rate from actual coupon value and period
                if nominal and float(nominal) > 0 and last_coupon["period_days"]:
                    coupon_rate = round(
                        (last_coupon["value"] / float(nominal))
                        * (365 / last_coupon["period_days"])
                        * 100,
                        4,
                    )
                    logger.debug(
                        "Floater %s: last coupon=%.4f period=%d days -> rate=%.2f%%",
                        secid, last_coupon["value"], last_coupon["period_days"], coupon_rate,
                    )
                elif coupon_period and float(coupon_period) > 0 and nominal and float(nominal) > 0:
                    coupon_rate = round(
                        (last_coupon["value"] / float(nominal))
                        * (365 / float(coupon_period))
                        * 100,
                        4,
                    )
        maturity_date = self._parse_date(sec_row.get("MATDATE"))
        buyback_date = self._parse_date(sec_row.get("BUYBACKDATE"))
        offer_date = self._parse_date(sec_row.get("OFFERDATE"))
        next_coupon_date = self._parse_date(sec_row.get("NEXTCOUPON"))
        aci = md_row.get("ACCINT") or sec_row.get("ACCRUEDINT")
        market_yield = md_row.get("YIELD") or sec_row.get("YIELDATPREVWAPRICE")
        company_rating = await self._get_smartlab_credit_rating(secid)
        rating_source = "smartlab" if company_rating else None
        if company_rating is None:
            company_rating = await self._get_credit_rating(secid)
            rating_source = "moex" if company_rating else None
        # Last resort: derive rating from MOEX listing level. This is a coarse
        # proxy, not an issuer rating — tagged as 'listlevel' so downstream code
        # never alerts on it nor persists it over a real rating.
        if company_rating is None:
            _listlevel_map = {1: "AA", 2: "BBB", 3: "BB"}
            listlevel = sec_row.get("LISTLEVEL")
            try:
                company_rating = _listlevel_map.get(int(listlevel)) if listlevel is not None else None
                rating_source = "listlevel" if company_rating else None
            except (TypeError, ValueError):
                pass

        is_qual, is_traded = await self._get_sec_meta(secid)

        # FX conversion for non-RUB bonds
        face_unit = sec_row.get("FACEUNIT") or "SUR"
        fx_rate = 1.0
        if face_unit != "SUR":
            rate = await self._get_fx_rate(face_unit)
            if rate is not None:
                fx_rate = rate
            else:
                logger.error(
                    "FX rate unavailable for %s, bond %s prices will be unconverted",
                    face_unit, secid,
                )

        # Convert absolute values from native currency to RUB.
        # NOTE: MOEX reports FACEVALUE and COUPONVALUE in the bond's own currency
        # (so they need ×fx_rate), but ACCRUEDINT (НКД) is ALREADY in rubles even
        # for FX bonds — multiplying it again double-converts and inflates the
        # position value and full-profit (НКД can exceed the nominal otherwise).
        nominal_rub = round(float(nominal) * fx_rate, 2) if nominal is not None else None
        coupon_rub = round(float(coupon) * fx_rate, 4) if coupon is not None else None
        aci_rub = round(float(aci), 5) if aci is not None else None

        snapshot = BondSnapshot(
            ticker=secid,
            name=str(name),
            clean_price_percent=float(clean_price_percent),
            prev_close_percent=prev_close_percent,
            nominal=nominal_rub,
            coupon=coupon_rub,
            coupon_period=(
                int(coupon_period)
                if coupon_period is not None
                else None
            ),
            coupon_rate=(
                float(coupon_rate)
                if coupon_rate is not None
                else None
            ),
            maturity_date=maturity_date,
            buyback_date=buyback_date,
            offer_date=offer_date,
            next_coupon_date=next_coupon_date,
            aci=aci_rub,
            market_yield=(
                float(market_yield)
                if market_yield is not None
                else None
            ),
            company_rating=company_rating,
            rating_source=rating_source,
            is_qual=is_qual,
            is_traded=is_traded,
            face_unit=face_unit,
            fx_rate=fx_rate,
            is_floater=is_floater,
        )
        self._snapshot_cache_put(self._bond_snapshot_cache, secid, snapshot)
        return snapshot

    async def _fetch(self, url: str) -> dict[str, Any]:
        """GET a MOEX ISS endpoint, retrying transient failures.

        MOEX briefly returns 502/500 or truncated JSON several times a day
        (1088x 502 and 76x 500 over three days on prod). A single blip used to
        wipe the price for every instrument in the refresh cycle: on 2026-08-10
        two such seconds produced 762 PriceNotFoundError, 353 of them bonds
        that had a working PREV* fallback. Only transient failures are retried
        — 4xx means a wrong URL or a missing security and will not change.
        """
        logger.debug("Fetching data from %s", url)
        src = self.sources["moex_price"]
        last_error: tuple[int | None, str] | None = None

        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()
                src.record_hit()
                return data
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code not in _RETRY_STATUS:
                    src.record_error(code, f"HTTP {code}")
                    logger.error("HTTP error %s while fetching %s", code, url)
                    raise DataFetchError(url, f"HTTP {code}") from exc
                last_error = (code, f"HTTP {code}")
            except httpx.RequestError as exc:
                # У ConnectTimeout и подобных str(exc) часто пустой — тогда в
                # логе оставалось "MOEX  (попытка 1/3)" без причины сбоя.
                last_error = (None, str(exc)[:80] or type(exc).__name__)
            except ValueError as exc:
                last_error = (None, "Invalid JSON")

            if attempt + 1 < _RETRY_ATTEMPTS:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "MOEX %s (попытка %d/%d), повтор через %.1fс: %s",
                    last_error[1], attempt + 1, _RETRY_ATTEMPTS, delay, url,
                )
                await asyncio.sleep(delay)

        code, message = last_error
        src.record_error(code, message)
        logger.error(
            "MOEX недоступен после %d попыток (%s): %s", _RETRY_ATTEMPTS, message, url
        )
        raise DataFetchError(url, message)

    @staticmethod
    def _get_first_row(dataset: dict[str, Any]) -> dict[str, Any]:
        columns = dataset.get("columns", [])
        rows = dataset.get("data", [])
        if not rows:
            return {}
        values = rows[0]
        max_index = min(len(columns), len(values))
        return {columns[idx]: values[idx] for idx in range(max_index)}

    @staticmethod
    def _get_row_with_price(dataset: dict[str, Any]) -> dict[str, Any]:
        """Return the first row where LAST or LCLOSE is set; fall back to first row."""
        columns = dataset.get("columns", [])
        rows = dataset.get("data", [])
        if not rows:
            return {}
        best = rows[0]
        for values in rows:
            max_index = min(len(columns), len(values))
            row = {columns[idx]: values[idx] for idx in range(max_index)}
            if row.get("LAST") is not None or row.get("LCLOSE") is not None:
                return row
        max_index = min(len(columns), len(best))
        return {columns[idx]: best[idx] for idx in range(max_index)}

    # Price fields a securities row may carry, in fallback order.
    _SEC_PRICE_FIELDS = (
        "PREVPRICE",
        "PREVWAPRICE",
        "PREVLEGALCLOSEPRICE",
    )

    @classmethod
    def _get_security_row(cls, dataset: dict[str, Any]) -> dict[str, Any]:
        """Pick the securities row that actually carries a price.

        MOEX may list a bond on several boards (e.g. SPOB + TQOB) and the
        first row can be a board with all PREV* prices empty. Blindly taking
        rows[0] then yields a zero-priced snapshot. Prefer the first row that
        has any PREV* price set; fall back to the first row otherwise.
        """
        columns = dataset.get("columns", [])
        rows = dataset.get("data", [])
        if not rows:
            return {}
        for values in rows:
            max_index = min(len(columns), len(values))
            row = {columns[idx]: values[idx] for idx in range(max_index)}
            if any(row.get(f) is not None for f in cls._SEC_PRICE_FIELDS):
                return row
        max_index = min(len(columns), len(rows[0]))
        return {columns[idx]: rows[0][idx] for idx in range(max_index)}

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    async def _get_credit_rating(self, secid: str) -> str | None:
        if secid in self._credit_rating_cache:
            return self._credit_rating_cache[secid]

        src = self.sources["moex_rating"]
        if not src.enabled:
            return None

        url = f"{settings.moex_base_url}/securities/{secid}/description.json"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            src.record_error(exc.response.status_code, f"HTTP {exc.response.status_code}")
            logger.warning(
                "MOEX description HTTP error %s for %s",
                exc.response.status_code,
                secid,
            )
            self._credit_rating_cache[secid] = None
            return None
        except httpx.RequestError as exc:
            src.record_error(None, str(exc)[:80])
            logger.warning("MOEX description request error for %s: %s", secid, exc)
            self._credit_rating_cache[secid] = None
            return None
        except ValueError:
            src.record_error(None, "Invalid JSON")
            logger.warning("Invalid JSON from MOEX description for %s", secid)
            self._credit_rating_cache[secid] = None
            return None

        dataset = data.get("description", {})
        columns = dataset.get("columns", [])
        rows = dataset.get("data", [])

        if not columns or not rows:
            src.record_miss()
            logger.debug("No description data for %s", secid)
            self._credit_rating_cache[secid] = None
            return None

        name_idx = self._find_column_index(columns, "name")
        title_idx = self._find_column_index(columns, "title")
        value_idx = self._find_column_index(columns, "value")

        if value_idx is None:
            src.record_miss()
            self._credit_rating_cache[secid] = None
            return None

        priority_keys = {
            "CREDITRATING",
            "CREDIT_RATING",
            "EMITTERCREDITRATING",
            "ISSUECREDITRATING",
            "RATING",
        }

        fallback: str | None = None
        for row in rows:
            key = self._safe_value(row, name_idx).upper()
            title = self._safe_value(row, title_idx).lower()
            value = self._safe_value(row, value_idx).strip()
            if not value:
                continue

            normalized = self._normalize_rating_value(value)

            if key in priority_keys and normalized is not None:
                src.record_hit()
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                "кредит" in title
                and "рейтинг" in title
                and normalized is not None
            ):
                src.record_hit()
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                "credit" in title
                and "rating" in title
                and normalized is not None
            ):
                src.record_hit()
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                ("рейтинг" in title or "rating" in title)
                and fallback is None
                and normalized is not None
            ):
                fallback = normalized

        if fallback is not None:
            src.record_hit()
        else:
            src.record_miss()
        self._credit_rating_cache[secid] = fallback
        return fallback

    async def _get_sec_meta(self, secid: str) -> tuple[bool, bool]:
        """Return (is_qual, is_traded) for a bond via MOEX securities API.

        Uses description.json (ISQUALIFIEDINVESTORS) and boards (is_traded on primary board).
        Results are cached in-memory.
        """
        if secid in self._is_qual_cache:
            return self._is_qual_cache[secid]

        url_desc = f"{settings.moex_base_url}/securities/{secid}/description.json?description.columns=name,value"
        url_boards = f"{settings.moex_base_url}/securities/{secid}.json?iss.only=boards&boards.columns=boardid,is_traded,is_primary"

        is_qual = False
        is_traded = True

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                desc_resp, boards_resp = await asyncio.gather(
                    client.get(url_desc),
                    client.get(url_boards),
                    return_exceptions=True,
                )

            if not isinstance(desc_resp, Exception):
                desc_resp.raise_for_status()
                desc = desc_resp.json().get("description", {})
                cols = desc.get("columns", [])
                for row in desc.get("data", []):
                    r = dict(zip(cols, row))
                    if r.get("name") == "ISQUALIFIEDINVESTORS":
                        is_qual = str(r.get("value", "0")) == "1"
                        break

            if not isinstance(boards_resp, Exception):
                boards_resp.raise_for_status()
                boards = boards_resp.json().get("boards", {})
                cols = boards.get("columns", [])
                for row in boards.get("data", []):
                    r = dict(zip(cols, row))
                    if r.get("is_primary") == 1:
                        is_traded = r.get("is_traded") == 1
                        break

        except Exception:
            pass  # default: not qual, is traded

        # Квал-статус и торгуемость почти статичны, TTL им не нужен — но и расти
        # бесконечно словарь не должен: по записи на каждый запрошенный secid.
        # При переполнении сбрасываем целиком: это метаданные, перечитать их
        # дешевле, чем тащить в память историю всех бумаг каталога.
        if len(self._is_qual_cache) >= self.SNAPSHOT_CACHE_MAX * 3:
            logger.debug("is_qual cache overflow (%d), сбрасываю", len(self._is_qual_cache))
            self._is_qual_cache.clear()
        self._is_qual_cache[secid] = (is_qual, is_traded)
        return is_qual, is_traded

    async def _get_smartlab_credit_rating(
        self, secid: str
    ) -> str | None:
        cache_key = f"smartlab:{secid}"
        if cache_key in self._credit_rating_cache:
            return self._credit_rating_cache[cache_key]

        src = self.sources["smartlab"]
        if not src.enabled:
            return None

        url = f"https://smart-lab.ru/q/bonds/{secid}/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0 Safari/537.36"
            )
        }

        # Short retries with backoff — a transient SmartLab timeout/5xx must not
        # freeze the rating onto the inaccurate LISTLEVEL proxy. On the final
        # failure cache with a short ERROR_TTL (set_error), not the 15min MISS_TTL.
        SMARTLAB_RETRY_DELAYS = [0.5, 1.5]  # seconds before each retry
        last_exc: Exception | None = None
        html: str | None = None
        for attempt in range(len(SMARTLAB_RETRY_DELAYS) + 1):
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    html = response.text
                break
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                code = exc.response.status_code
                # 404 is a definitive "no such bond" — don't waste retries on it.
                if code == 404:
                    src.record_error(code, f"HTTP {code}")
                    logger.warning("SmartLab HTTP 404 for %s", secid)
                    self._credit_rating_cache.set_error(cache_key)
                    return None
                if attempt < len(SMARTLAB_RETRY_DELAYS):
                    logger.warning(
                        "SmartLab HTTP %s for %s (attempt %d) — retrying",
                        code, secid, attempt + 1,
                    )
                    await asyncio.sleep(SMARTLAB_RETRY_DELAYS[attempt])
                    continue
                src.record_error(code, f"HTTP {code}")
                logger.warning("SmartLab HTTP error %s for %s", code, secid)
                self._credit_rating_cache.set_error(cache_key)
                return None
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < len(SMARTLAB_RETRY_DELAYS):
                    logger.warning(
                        "SmartLab request error for %s (attempt %d): %s — retrying",
                        secid, attempt + 1, exc,
                    )
                    await asyncio.sleep(SMARTLAB_RETRY_DELAYS[attempt])
                    continue
                src.record_error(None, str(exc)[:80])
                logger.warning("SmartLab request error for %s: %s", secid, exc)
                self._credit_rating_cache.set_error(cache_key)
                return None

        if html is None:  # defensive: shouldn't happen, all paths above return
            src.record_error(None, str(last_exc)[:80] if last_exc else "")
            self._credit_rating_cache.set_error(cache_key)
            return None

        rating_match = self._extract_rating_from_html(html)

        if rating_match is None:
            src.record_miss()
            logger.debug("Rating not found for %s on SmartLab", secid)
            self._credit_rating_cache[cache_key] = None
            return None

        src.record_hit()
        rating, rating_pos = rating_match
        rating_date = self._find_nearest_dotted_date(html, rating_pos)

        result = rating if rating_date is None else f"{rating} ({rating_date})"
        self._credit_rating_cache[cache_key] = result
        return result

    @staticmethod
    def _extract_rating_from_html(text: str) -> tuple[str, int] | None:
        """Кредитный рейтинг со страницы SmartLab — только из размеченного блока.

        Раньше при неудаче был второй проход «найти букву рядом со словом
        рейтинг». В подвале сайта есть ссылка «Рейтинг брокеров», буква из
        соседнего слова попадала в окно ±120 символов — и проход возвращал
        "A" для ЛЮБОЙ страницы. У ОФЗ рейтинга на странице нет вовсе, они
        получали фальшивый "A" (105 записей в rating_history), а один раз
        "D" — дефолт у госбумаги. Отсутствие рейтинга честнее выдумки.
        """
        return MOEXService._find_rating_with_label(text)

    @staticmethod
    def _find_rating_with_label(text: str) -> tuple[str, int] | None:
        class_pattern = re.compile(
            r"linear-progress-bar__text[^>]*>\s*"
            r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|"
            r"B[+-]?|CCC|CC|C|D|"
            r"ru(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|"
            r"B[+-]?|CCC|CC|C|D)(?:\(EXP\))?)\s*<",
            re.IGNORECASE,
        )
        class_match = class_pattern.search(text)
        if class_match is not None:
            normalized = MOEXService._normalize_rating_value(
                class_match.group(1)
            )
            if normalized is not None:
                return normalized, class_match.start(1)

        pattern = re.compile(
            r"кредитн\w*\s+рейтинг[\s\S]{0,300}?"
            r"(ru(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|"
            r"B[+-]?|CCC|CC|C|D)(?:\(EXP\))?)",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if match is None:
            return None
        normalized = MOEXService._normalize_rating_value(match.group(1))
        if normalized is None:
            return None
        return normalized, match.start(1)

    @staticmethod
    def _find_nearest_dotted_date(
        text: str, pivot_position: int
    ) -> str | None:
        pattern = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
        matches = list(pattern.finditer(text))
        if not matches:
            return None

        nearest = min(
            matches,
            key=lambda item: abs(item.start(1) - pivot_position),
        )
        if abs(nearest.start(1) - pivot_position) > 500:
            return None
        return nearest.group(1)

    @staticmethod
    def _find_column_index(columns: list[str], name: str) -> int | None:
        for index, column in enumerate(columns):
            if column.lower() == name:
                return index
        return None

    @staticmethod
    def _safe_value(row: list[Any], index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        value = row[index]
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _normalize_rating_value(value: str) -> str | None:
        text = value.strip()
        if text.upper() == "RUB":
            return None
        match = MOEXService.RATING_PATTERN.fullmatch(text)
        if match is not None:
            # Strip leading "ru"/"RU" prefix (e.g. ruAA+ → AA+)
            raw = match.group(1)
            return re.sub(r"(?i)^ru(?=[A-Z])", "", raw).upper().replace("(EXP)", "")

        bare_match = MOEXService.BARE_RATING_PATTERN.fullmatch(text)
        if bare_match is not None:
            return bare_match.group(1).upper()

        return None


moex_service = MOEXService()
