"""The completeness gate every account country's ``parse_feed`` opens with.

A failed or partial fetch must never be published as "fewer parcels", so each
feed is checked against the count the backend states before any row is read.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import GlsAccountFeedError

_LOGGER = logging.getLogger(__name__)


def complete_items(feed: Any, count_key: str) -> list[dict]:
    """Return the feed's item dicts, or raise unless all of them arrived.

    ``count_key`` names the backend's own total for the list; it must be a
    non-negative int equal to the number of items returned.
    """
    if not isinstance(feed, dict):
        raise GlsAccountFeedError("GLS account returned an invalid parcel list")
    items, count = feed.get("items"), feed.get(count_key)
    if (
        not isinstance(items, list)
        or type(count) is not int
        or count < 0
        or count != len(items)
    ):
        _LOGGER.debug(
            "GLS account list rejected: %s is %s %r, items is %s%s",
            count_key,
            type(count).__name__,
            count,
            type(items).__name__,
            f" with {len(items)} entries" if isinstance(items, list) else "",
        )
        raise GlsAccountFeedError("GLS account returned an incomplete parcel list")
    if not all(isinstance(item, dict) for item in items):
        raise GlsAccountFeedError("GLS account returned an invalid parcel")
    return items
