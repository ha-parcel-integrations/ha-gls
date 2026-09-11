"""GLS US: keyless public POST transport, shipment selection and mapping.

Two things make the US its own package rather than another row pointing at an
existing transport. The request is a ``POST`` with a JSON body against a host
none of the other backends use, and one user-facing tracking number can resolve
to **several** shipment records (a return-to-sender followed by its
replacement, for example). The response lists them oldest first, so array
position must never decide which one the sensor shows — the newest record does,
and the newest is determined from its own timestamps (see
:func:`_recency_key`).

The status vocabulary is only partly captured, so mapping here is
**derivation-first**, the way ``countries/de/`` is: the exact-match table below
holds only the delivered literal the carrier's own frontend keys off, and
anything else falls back to what the shipment's dates and transit events prove
happened. Every unrecognised status text still logs once with the
unrecognised-status issue link, which is how the table gets filled in without a
second build session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    GLS_US_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Only the literal the carrier's own tracker keys off is mapped exactly. Every
# other text is derived from the shipment's dates instead of guessed from
# English wording — see this module's docstring.
_STATUS_MAP = {
    "shipment delivered": ParcelStatus.DELIVERED,
    "delivered": ParcelStatus.DELIVERED,
}

_unmapped_statuses_logged: set[str] = set()
_unexpected_shapes_logged: set[str] = set()
_unparseable_timestamps_logged: set[str] = set()

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# The .NET sentinels the backend sends for "no such date". They parse fine, so
# they have to be rejected by value or a sentinel would win a recency
# comparison and become a delivery timestamp.
_SENTINEL_YEARS = (1, 1900)

_ACCEPT_LANGUAGE = "en-US"

# Older than any real event, so a shipment with no usable timestamp at all
# never outranks one that has them.
_NO_TIMESTAMP = datetime.min.replace(tzinfo=timezone.utc)


def _normalise_status(value: object) -> str | None:
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _warn_unmapped_status(value: object) -> None:
    text = str(value)
    if text in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(text)
    _LOGGER.warning(
        "Unrecognised GLS US status — help us map it. Open an issue and paste "
        "this line: %s\n  status=%r → falling back to the derived status",
        _NEW_ISSUE_URL,
        value,
    )


def map_parcel_status_us(value: object) -> ParcelStatus:
    """Map a US status text, or ``UNKNOWN`` when it is not in the table.

    ``UNKNOWN`` here means "not captured yet", not "nothing is known about this
    parcel" — :func:`normalize_parcel_us` then derives the status from the
    shipment's own dates.
    """
    normalised = _normalise_status(value)
    if normalised is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(normalised)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(value)
    return ParcelStatus.UNKNOWN


def map_event_status_us(value: object) -> ParcelStatus | None:
    """Map a US transit-event text, keeping unknown history events neutral.

    A history entry has no dates of its own to derive from, so an unmapped
    event text stays ``None`` rather than guessing.
    """
    normalised = _normalise_status(value)
    if normalised is None:
        return None
    mapped = _STATUS_MAP.get(normalised)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(value)
    return None


def _warn_shape(path: str, value: object) -> None:
    marker = f"{path}: {type(value).__name__}"
    if marker in _unexpected_shapes_logged:
        return
    _unexpected_shapes_logged.add(marker)
    _LOGGER.warning("GLS US response has an unexpected field shape: %s", marker)


def _parse_datetime(value: object) -> datetime | None:
    """Parse a US timestamp, treating the .NET sentinel dates as absent.

    The backend sends ISO-8601 without an offset. GLS's existing DE and CA
    transports treat the same offset-free shape as UTC so the shared retention
    and sorting helpers can operate on it; US follows that convention until the
    carrier publishes a timezone field.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        if value not in _unparseable_timestamps_logged:
            _unparseable_timestamps_logged.add(value)
            _LOGGER.warning("GLS US returned an unparseable timestamp format")
        return None
    if parsed.year in _SENTINEL_YEARS:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: object) -> str | None:
    """Return a US timestamp as a canonical ISO string, or ``None``."""
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else None


def _transit_details(shipment: dict) -> list[dict]:
    details = shipment.get("transitDetails")
    if details is None:
        return []
    if not isinstance(details, list):
        _warn_shape("shipments[].transitDetails", details)
        return []
    return [detail for detail in details if isinstance(detail, dict)]


def _event_datetimes(shipment: dict) -> list[datetime]:
    return [
        parsed
        for detail in _transit_details(shipment)
        if (parsed := _parse_datetime(detail.get("eventDateTime"))) is not None
    ]


def _recency_key(shipment: dict) -> datetime:
    """How recent a shipment record is, for picking between duplicates.

    The most recent transit event wins; a record with none falls back to its
    delivery date and then its ship date. Everything is a sentinel-checked
    parse, so a placeholder date can never make a stale record look current.
    """
    events = _event_datetimes(shipment)
    if events:
        return max(events)
    for field in ("deliveryDate", "shipDate"):
        parsed = _parse_datetime(shipment.get(field))
        if parsed is not None:
            return parsed
    return _NO_TIMESTAMP


def _select_shipment(raw: dict, parcel_no: str | None) -> dict | None:
    """Return the newest shipment record for ``parcel_no``.

    Only records whose ``trackingNumber`` matches the tracked code are
    considered — the lookup asks about one number, so a record for another one
    is an unexpected shape, never a parcel to fall back on.
    """
    shipments = raw.get("shipments")
    if not isinstance(shipments, list):
        _warn_shape("shipments", shipments)
        return None
    target = (parcel_no or "").casefold()
    matches: list[tuple[datetime, int, dict]] = []
    for index, shipment in enumerate(shipments):
        if not isinstance(shipment, dict):
            _warn_shape("shipments[]", shipment)
            continue
        number = shipment.get("trackingNumber")
        if isinstance(number, str) and number.casefold() == target:
            # The index breaks a tie towards the later record: the response is
            # chronological, so the last of two equally-stamped records is the
            # newer one.
            matches.append((_recency_key(shipment), index, shipment))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


async def async_get_parcel_us(
    session: aiohttp.ClientSession, parcel_no: str, postal_code: str
) -> dict[str, Any] | None:
    """Fetch a US shipment from the keyless public tracking API.

    ``postal_code`` is accepted so every country's transport keeps the same
    signature, but the US lookup is keyed on the tracking number alone and
    never sends it — see ``GLS_US_TRACKING_URL``.
    """
    del postal_code
    async with session.post(
        GLS_US_TRACKING_URL,
        json={"trackingNumbers": parcel_no, "isFreight": False},
        headers={"Accept-Language": _ACCEPT_LANGUAGE},
    ) as response:
        status = response.status
        # The frontend reads an empty body as "invalid or not yet available",
        # so a 204 is a semantic not-found, not a transport failure.
        if status == 204:
            return None
        if status != 200:
            raise GlsApiError(status)
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            _LOGGER.warning("GLS US returned an unparseable JSON body")
            return None
    if not isinstance(body, dict):
        _warn_shape("$", body)
        return None
    if _select_shipment(body, parcel_no) is None:
        return None
    return body


def _derive_status(shipment: dict) -> ParcelStatus:
    """Work out the status from what the shipment's own fields prove.

    Used whenever the carrier's status text is not in ``_STATUS_MAP`` — which
    is most texts, the vocabulary being only partly captured. Deliberately
    coarse: a delivery date means delivered, a scan means it is moving, a ship
    date alone means it is announced. Nothing here guesses a return, an
    exception or a pickup point from English wording.
    """
    if _parse_datetime(shipment.get("deliveryDate")) is not None:
        return ParcelStatus.DELIVERED
    if _event_datetimes(shipment):
        return ParcelStatus.IN_TRANSIT
    if _parse_datetime(shipment.get("shipDate")) is not None:
        return ParcelStatus.REGISTERED
    return ParcelStatus.UNKNOWN


def _history(shipment: dict) -> list[dict]:
    events: list[tuple[datetime, dict]] = []
    for detail in _transit_details(shipment):
        parsed = _parse_datetime(detail.get("eventDateTime"))
        if parsed is None:
            continue
        events.append(
            (
                parsed,
                {
                    "timestamp": parsed.isoformat(),
                    "status": map_event_status_us(detail.get("eventDetails")),
                    "raw_status": detail.get("eventDetails"),
                },
            )
        )
    events.sort(key=lambda item: item[0])
    return [event for _, event in events][-HISTORY_MAX_EVENTS:]


def _delivered_at(shipment: dict) -> str | None:
    """Return the carrier's delivery date, falling back to its last scan.

    ``deliveredAt`` is deliberately not consulted — it has only been seen
    carrying drop-off text, not a timestamp.
    """
    delivered = _parse_timestamp(shipment.get("deliveryDate"))
    if delivered is not None:
        return delivered
    events = _event_datetimes(shipment)
    return max(events).isoformat() if events else None


def normalize_parcel_us(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "US",
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Normalize a GLS US response while preserving its full ``raw`` body."""
    shipment = _select_shipment(raw, parcel_no) or {}
    status_name = shipment.get("status")
    status = map_parcel_status_us(status_name)
    if status == ParcelStatus.UNKNOWN:
        status = _derive_status(shipment)
    barcode = shipment.get("trackingNumber") or parcel_no
    template = COUNTRIES.get(country, {}).get("tracking_url") or TRACKING_URL
    url = template.format(parcel_no=barcode) if barcode else None

    return {
        "carrier": "GLS",
        "barcode": barcode,
        # The response's only party field is the recipient's own delivery
        # address, which is not a sender/receiver display name. It stays in
        # ``raw`` rather than being reinterpreted as one.
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_name,
        "delivered": status == ParcelStatus.DELIVERED,
        "delivered_at": (
            _delivered_at(shipment) if status == ParcelStatus.DELIVERED else None
        ),
        # estimatedDeliveryDate is a date, not the ETA *window* the contract's
        # planned_from/planned_to describe — see CAPABILITIES_BY_VARIANT.
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": url,
        "weight": None,
        "dimensions": None,
        "history": _history(shipment) if include_history else None,
        "raw": raw,
    }


__all__ = [
    "async_get_parcel_us",
    "map_event_status_us",
    "map_parcel_status_us",
    "normalize_parcel_us",
]
