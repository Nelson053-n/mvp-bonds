from datetime import date
import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.models import BondSnapshot, StockSnapshot
from app.exceptions import PriceNotFoundError, DataFetchError, RatingNotFoundError

logger = logging.getLogger(__name__)


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

    def __init__(self) -> None:
        self._credit_rating_cache: dict[str, str | None] = {}

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
            f"bonds/boards/TQCB/securities/{secid}.json"
        )
        data = await self._fetch(url)

        sec_row = self._get_first_row(data.get("securities", {}))
        md_row = self._get_first_row(data.get("marketdata", {}))

        name = sec_row.get("SHORTNAME") or sec_row.get("SECNAME") or secid
        clean_price_percent = md_row.get("LAST") or md_row.get("LCLOSE")
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
        aci = md_row.get("ACCINT")
        market_yield = md_row.get("YIELD")
        company_rating = await self._get_smartlab_credit_rating(secid)
        if company_rating is None:
            company_rating = await self._get_credit_rating(secid)

        return BondSnapshot(
            ticker=secid,
            name=str(name),
            clean_price_percent=float(clean_price_percent),
            nominal=float(nominal) if nominal is not None else None,
            coupon=float(coupon) if coupon is not None else None,
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
            aci=float(aci) if aci is not None else None,
            market_yield=(
                float(market_yield)
                if market_yield is not None
                else None
            ),
            company_rating=company_rating,
        )

    async def _fetch(self, url: str) -> dict[str, Any]:
        logger.debug("Fetching data from %s", url)
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.error("HTTP error %s while fetching %s", exc.response.status_code, url)
                raise DataFetchError(url, f"HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                logger.error("Request error while fetching %s: %s", url, exc)
                raise DataFetchError(url, str(exc)) from exc
            except ValueError as exc:
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
    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            year, month, day = value.split("-")
            return date(int(year), int(month), int(day))
        except Exception:
            return None

    async def _get_credit_rating(self, secid: str) -> str | None:
        if secid in self._credit_rating_cache:
            return self._credit_rating_cache[secid]

        url = f"{settings.moex_base_url}/securities/{secid}/description.json"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "MOEX description HTTP error %s for %s",
                exc.response.status_code,
                secid,
            )
            self._credit_rating_cache[secid] = None
            return None
        except httpx.RequestError as exc:
            logger.warning("MOEX description request error for %s: %s", secid, exc)
            self._credit_rating_cache[secid] = None
            return None
        except ValueError:
            logger.warning("Invalid JSON from MOEX description for %s", secid)
            self._credit_rating_cache[secid] = None
            return None

        dataset = data.get("description", {})
        columns = dataset.get("columns", [])
        rows = dataset.get("data", [])

        if not columns or not rows:
            logger.debug("No description data for %s", secid)
            self._credit_rating_cache[secid] = None
            return None

        name_idx = self._find_column_index(columns, "name")
        title_idx = self._find_column_index(columns, "title")
        value_idx = self._find_column_index(columns, "value")

        if value_idx is None:
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
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                "кредит" in title
                and "рейтинг" in title
                and normalized is not None
            ):
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                "credit" in title
                and "rating" in title
                and normalized is not None
            ):
                self._credit_rating_cache[secid] = normalized
                return normalized

            if (
                ("рейтинг" in title or "rating" in title)
                and fallback is None
                and normalized is not None
            ):
                fallback = normalized

        self._credit_rating_cache[secid] = fallback
        return fallback

    async def _get_smartlab_credit_rating(
        self, secid: str
    ) -> str | None:
        cache_key = f"smartlab:{secid}"
        if cache_key in self._credit_rating_cache:
            return self._credit_rating_cache[cache_key]

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
            logger.warning(
                "SmartLab HTTP error %s for %s",
                exc.response.status_code,
                secid,
            )
            self._credit_rating_cache[cache_key] = None
            return None
        except httpx.RequestError as exc:
            logger.warning("SmartLab request error for %s: %s", secid, exc)
            self._credit_rating_cache[cache_key] = None
            return None

        rating_match = self._find_rating_with_label(html)
        if rating_match is None:
            rating_match = self._find_rating_anywhere(html)

        if rating_match is None:
            logger.debug("Rating not found for %s on SmartLab", secid)
            self._credit_rating_cache[cache_key] = None
            return None

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
