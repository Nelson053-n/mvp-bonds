"""
Custom exceptions for the application.
"""


class AppError(Exception):
    """Base exception for the application."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.message = message
        self.detail = detail or message
        super().__init__(self.message)


class ValidationError(AppError):
    """Raised when input validation fails."""


class NotFoundError(AppError):
    """Raised when a requested resource is not found."""


class MOEXError(AppError):
    """Base exception for MOEX service errors."""


class PriceNotFoundError(MOEXError):
    """Raised when market price cannot be retrieved."""

    def __init__(self, ticker: str, instrument_type: str) -> None:
        message = f"Не удалось получить цену для {ticker}"
        detail = f"Не удалось получить рыночную цену {instrument_type} {ticker}"
        super().__init__(message, detail)


class DataFetchError(MOEXError):
    """Raised when data fetching from MOEX fails."""

    def __init__(self, ticker: str, reason: str | None = None) -> None:
        message = f"Ошибка получения данных для {ticker}"
        detail = f"{message}: {reason}" if reason else message
        super().__init__(message, detail)


class SmartLabError(AppError):
    """Base exception for SmartLab service errors."""


class RatingNotFoundError(SmartLabError):
    """Raised when credit rating cannot be found."""

    def __init__(self, ticker: str) -> None:
        message = f"Рейтинг не найден для {ticker}"
        super().__init__(message)


class PortfolioError(AppError):
    """Base exception for portfolio service errors."""


class InstrumentNotFoundError(PortfolioError):
    """Raised when portfolio instrument is not found."""

    def __init__(self, item_id: int) -> None:
        message = f"Инструмент с ID {item_id} не найден"
        super().__init__(message)


class CacheError(AppError):
    """Base exception for cache service errors."""
