"""Unit tests for app.services.rating_utils (rating rank + worsening check).

These functions drive the double-downgrade Telegram alert, so the edge cases that
matter are: unknown ratings (None / off-scale strings), the full ordering of the
+/- modifier scale, and the strict "worse than" comparison (an unchanged rating is
NOT a downgrade).
"""

import pytest

from app.services.rating_utils import (
    _RATING_ORDER,
    rating_rank,
    rating_worsened,
)


class TestRatingRank:
    """rating_rank: lower number = better rating; None → 999, off-scale → 998."""

    def test_aaa_is_best(self):
        assert rating_rank("AAA") == 0

    def test_d_is_worst_on_scale(self):
        assert rating_rank("D") == len(_RATING_ORDER) - 1

    def test_none_is_999(self):
        assert rating_rank(None) == 999

    def test_unknown_string_is_998(self):
        assert rating_rank("ruAAA") == 998
        assert rating_rank("garbage") == 998
        assert rating_rank("") == 998

    def test_none_ranks_worse_than_off_scale(self):
        """None (999) is treated as worse/unknown than an off-scale label (998)."""
        assert rating_rank(None) > rating_rank("unknown-label")

    @pytest.mark.parametrize("rating", _RATING_ORDER)
    def test_every_scale_rating_is_known(self, rating):
        assert rating_rank(rating) < 900

    def test_modifiers_are_ordered(self):
        """+ outranks plain outranks - within a letter grade."""
        assert rating_rank("AA+") < rating_rank("AA") < rating_rank("AA-")
        assert rating_rank("BBB+") < rating_rank("BBB") < rating_rank("BBB-")

    def test_letter_grades_descend(self):
        """A whole grade is better than the one below it."""
        assert rating_rank("A-") < rating_rank("BBB+")
        assert rating_rank("BBB-") < rating_rank("BB+")

    def test_case_sensitive_lowercase_is_off_scale(self):
        """The table is uppercase; a lowercased rating is not recognised."""
        assert rating_rank("aaa") == 998


class TestRatingWorsened:
    """rating_worsened: strictly worse curr vs prev."""

    def test_downgrade_is_true(self):
        assert rating_worsened("AAA", "AA+") is True
        assert rating_worsened("A", "BBB") is True

    def test_upgrade_is_false(self):
        assert rating_worsened("AA", "AAA") is False
        assert rating_worsened("BBB", "A") is False

    def test_unchanged_is_false(self):
        """Same rating is not a downgrade (strict comparison)."""
        assert rating_worsened("AA", "AA") is False

    def test_single_notch_downgrade(self):
        assert rating_worsened("AA+", "AA") is True

    def test_known_to_none_is_worse(self):
        """Losing a rating (known → None, 999) counts as worsening."""
        assert rating_worsened("AAA", None) is True

    def test_none_to_known_is_improvement(self):
        """Gaining a rating (None → known) is not a downgrade."""
        assert rating_worsened(None, "AAA") is False

    def test_none_to_none_is_false(self):
        assert rating_worsened(None, None) is False

    def test_off_scale_to_none_is_worse(self):
        """Off-scale (998) → None (999) is a (slight) worsening by the rank model."""
        assert rating_worsened("ruBBB", None) is True

    def test_known_to_off_scale_is_worse(self):
        """A recognised rating dropping to an unparseable label ranks as worse."""
        assert rating_worsened("AAA", "ruAAA") is True
