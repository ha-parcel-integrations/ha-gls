"""Timestamp parsing shared across country modules and the generic list helpers.

Free of other integration modules so it can be imported by
``parcels.py`` (the generic, country-agnostic sort/filter helpers) *and* by
every ``countries/<code>.py`` module without creating an import cycle between
them.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util


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
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# Every country here spans one civil zone for the parcels GLS serves. A
# country missing from the map (or spanning several zones) falls back to the
# user's Home Assistant zone, which is the best guess left.
_COUNTRY_ZONES = {
    "AT": ZoneInfo("Europe/Vienna"),
    "BE": ZoneInfo("Europe/Brussels"),
    "CH": ZoneInfo("Europe/Zurich"),
    "CZ": ZoneInfo("Europe/Prague"),
    "DE": ZoneInfo("Europe/Berlin"),
    "DK": ZoneInfo("Europe/Copenhagen"),
    "FI": ZoneInfo("Europe/Helsinki"),
    "FR": ZoneInfo("Europe/Paris"),
    "HR": ZoneInfo("Europe/Zagreb"),
    "HU": ZoneInfo("Europe/Budapest"),
    "IE": ZoneInfo("Europe/Dublin"),
    "IT": ZoneInfo("Europe/Rome"),
    "LI": ZoneInfo("Europe/Vaduz"),
    "LU": ZoneInfo("Europe/Luxembourg"),
    "NL": ZoneInfo("Europe/Amsterdam"),
    "RO": ZoneInfo("Europe/Bucharest"),
    "RS": ZoneInfo("Europe/Belgrade"),
    "SE": ZoneInfo("Europe/Stockholm"),
    "SI": ZoneInfo("Europe/Ljubljana"),
    "SK": ZoneInfo("Europe/Bratislava"),
}


def country_zone(country: str | None) -> tzinfo:
    """Return the zone a carrier's naive local times are written in."""
    zone = _COUNTRY_ZONES.get(country or "")
    if zone:
        return zone
    return dt_util.get_default_time_zone()


def localize_naive(value: datetime, zone: tzinfo) -> datetime:
    """Give a naive time ``zone``; an aware time keeps its offset."""
    return value if value.tzinfo else value.replace(tzinfo=zone)
