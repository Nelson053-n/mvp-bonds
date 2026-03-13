"""Rating comparison utilities for credit rating monitoring."""

_RATING_ORDER = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]


def rating_rank(r: str | None) -> int:
    """Lower rank = better rating. None → 999 (unknown)."""
    if r is None:
        return 999
    return _RATING_ORDER.index(r) if r in _RATING_ORDER else 998


def rating_worsened(prev: str | None, curr: str | None) -> bool:
    """Return True if curr is strictly worse than prev."""
    return rating_rank(curr) > rating_rank(prev)
