"""The completeness gate shared by every account country's feed parser."""

from __future__ import annotations

import pytest

from custom_components.gls.account.errors import GlsAccountFeedError
from custom_components.gls.account.feed import complete_items


def test_complete_feed_returns_its_items():
    items = [{"a": 1}, {"b": 2}]
    assert complete_items({"items": items, "total": 2}, "total") == items
    assert complete_items({"items": [], "total": 0}, "total") == []


@pytest.mark.parametrize(
    "feed",
    [
        None,
        "not a dict",
        {"items": "nope", "total": 0},
        {"items": [{}], "total": 2},
        {"items": [{}], "total": True},
        {"items": [{}], "total": "1"},
        {"items": [], "total": -1},
        {"items": [{}]},
        {"items": ["x"], "total": 1},
    ],
)
def test_invalid_or_incomplete_feed_is_refused(feed):
    with pytest.raises(GlsAccountFeedError):
        complete_items(feed, "total")
