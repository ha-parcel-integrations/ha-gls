"""Timestamp parsing shared across country modules and the generic list helpers.

Deliberately dependency-free (stdlib only) so it can be imported by
``parcels.py`` (the generic, country-agnostic sort/filter helpers) *and* by
every ``countries/<code>.py`` module without creating an import cycle between
them.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing
    on a mixed set. The canonical parcel contract stores every carrier's
    timestamps as ISO strings regardless of the wire format they arrived in
    — a country module that receives a non-ISO timestamp (e.g. GLS Germany's
    ``"%Y-%m-%d %H:%M:%S"``) must convert it before this function ever sees
    it.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
