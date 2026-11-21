"""Pure helpers shared by every account country's normalizer.

Kept here, not duplicated per country, because the rules for what may enter a
published ``raw`` attribute are the same whatever the account's country.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


def scalar(value: Any) -> bool:
    """Known fields carry scalars, never unreviewed nested credentials."""
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value is None or isinstance(value, (str, int, float, bool))


def text(value: Any) -> str | None:
    """Accept only a nonempty human-readable scalar string."""
    return value.strip() or None if isinstance(value, str) else None


def pick(source: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Copy only allowlisted scalar fields — the one gate into ``raw``."""
    return {
        key: value for key, value in source.items() if key in allowed and scalar(value)
    }
