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

    FX_RATE_TTL = 3600  # 1 hour cache for FX rates

    def __init__(self) -> None:
        self._credit_rating_cache: dict[str, str | None] = {}
        self._is_qual_cache: dict[str, bool] = {}
        self._fx_rate_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate, timestamp)
        self.sources: dict[str, SourceStats] = {
            "moex_price": SourceStats("moex_price", "MOEX ISS (цены)"),
            "moex_rating": SourceStats("moex_rating", "MOEX ISS (рейтинг)"),
            "smartlab": SourceStats("smartlab", "Smart-Lab (рейтинг)"),
            "moex_fx": SourceStats("moex_fx", "MOEX ISS (валюты)"),
        }

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

    async def get_stock_snapshot(self, ticker: str) -> StockSnapshot:
        secid = ticker.upper().strip()
        url = (
            f"{settings.moex_base_url}/engines/stock/markets/"
            f"shares/boards/TQBR/securities/{secid}.json"
        )
        data = await self._fetch(url)

        sec_row = self._get_first_row(data.get("securities", {}))
        md_row = self._get_first_row(data.get("marketdata", {}))

        name = sec_row.get("SHORTNAME") or sec_row.get("SECNAME") or secid
        current_price = md_row.get("LAST") or md_row.get("LCLOSE")
        if not current_price:
            logger.error("Не удалось получить цену акции %s", secid)
            raise PriceNotFoundError(secid, "акция")
        company_rating = await self._get_credit_rating(secid)

        return StockSnapshot(
            ticker=secid,
            name=str(name),
            current_price=float(current_price),
            dividend_yield=None,
            company_rating=company_rating,
        )

    async def get_bond_snapshot(self, ticker: str) -> BondSnapshot:
        secid = ticker.upper().strip()
        url = (
            f"{settings.moex_base_url}/engines/stock/markets/"
            f"bonds/securities/{secid}.json"
        )
        data = await self._fetch(url)

        sec_row = self._get_first_row(data.get("securities", {}))
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
            logger.error("Не удалось получить цену облигации %s", secid)
            raise PriceNotFoundError(secid, "облигация")

        nominal = sec_row.get("FACEVALUE")
        coupon = sec_row.get("COUPONVALUE")
        coupon_period = sec_row.get("COUPONPERIOD")
        coupon_rate = sec_row.get("COUPONPERCENT")  # Ставка купона в %
        maturity_date = self._parse_date(sec_row.get("MATDATE"))
        buyback_date = self._parse_date(sec_row.get("BUYBACKDATE"))
        offer_date = self._parse_date(sec_row.get("OFFERDATE"))
        next_coupon_date = self._parse_date(sec_row.get("NEXTCOUPON"))
        aci = md_row.get("ACCINT") or sec_row.get("ACCRUEDINT")
        market_yield = md_row.get("YIELD") or sec_row.get("YIELDATPREVWAPRICE")
        company_rating = await self._get_smartlab_credit_rating(secid)
        if company_rating is None:
            company_rating = await self._get_credit_rating(secid)
        # Last resort: derive rating from MOEX listing level
        if company_rating is None:
            _listlevel_map = {1: "AA", 2: "BBB", 3: "BB"}
            listlevel = sec_row.get("LISTLEVEL")
            try:
                company_rating = _listlevel_map.get(int(listlevel)) if listlevel is not None else None
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

        # Convert absolute values from native currency to RUB
        nominal_rub = round(float(nominal) * fx_rate, 2) if nominal is not None else None
        coupon_rub = round(float(coupon) * fx_rate, 4) if coupon is not None else None
        aci_rub = round(float(aci) * fx_rate, 5) if aci is not None else None

        return BondSnapshot(
            ticker=secid,
            name=str(name),
            clean_price_percent=float(clean_price_percent),
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
            is_qual=is_qual,
            is_traded=is_traded,
            face_unit=face_unit,
            fx_rate=fx_rate,
        )

    async def _fetch(self, url: str) -> dict[str, Any]:
        logger.debug("Fetching data from %s", url)
        src = self.sources["moex_price"]
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                src.record_hit()
                return response.json()
            except httpx.HTTPStatusError as exc:
                src.record_error(exc.response.status_code, f"HTTP {exc.response.status_code}")
                logger.error("HTTP error %s while fetching %s", exc.response.status_code, url)
                raise DataFetchError(url, f"HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                src.record_error(None, str(exc)[:80])
                logger.error("Request error while fetching %s: %s", url, exc)
                raise DataFetchError(url, str(exc)) from exc
            except ValueError as exc:
                src.record_error(None, "Invalid JSON")
                logger.error("Invalid JSON response from %s", url)
                raise DataFetchError(url, "Invalid JSON response") from exc

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

        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                html = response.text
        except httpx.HTTPStatusError as exc:
            src.record_error(exc.response.status_code, f"HTTP {exc.response.status_code}")
            logger.warning(
                "SmartLab HTTP error %s for %s",
                exc.response.status_code,
                secid,
            )
            self._credit_rating_cache[cache_key] = None
            return None
        except httpx.RequestError as exc:
            src.record_error(None, str(exc)[:80])
            logger.warning("SmartLab request error for %s: %s", secid, exc)
            self._credit_rating_cache[cache_key] = None
            return None

        rating_match = self._find_rating_with_label(html)
        if rating_match is None:
            rating_match = self._find_rating_anywhere(html)

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
    def _find_rating_anywhere(text: str) -> tuple[str, int] | None:
        pattern = re.compile(
            r"\b(ru(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|"
            r"B[+-]?|CCC|CC|C|D)(?:\(EXP\))?|"
            r"AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|"
            r"CCC|CC|C|D)\b",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            normalized = MOEXService._normalize_rating_value(match.group(1))
            if normalized is None:
                continue

            start = max(0, match.start(1) - 120)
            end = min(len(text), match.end(1) + 120)
            window = text[start:end].lower()
            if "рейтинг" in window or "rating" in window:
                return normalized, match.start(1)

        return None

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
