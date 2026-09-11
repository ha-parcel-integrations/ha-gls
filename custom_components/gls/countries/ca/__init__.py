"""GLS Canada / Dicom: postcode-enhanced public tracking transport and mapping."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    GLS_CA_TRACKING_DETAILS_URL,
    GLS_CA_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_STATUS_MAP = {
    "shipment created": ParcelStatus.REGISTERED,
    "information received": ParcelStatus.REGISTERED,
    "created": ParcelStatus.REGISTERED,
    "picked up": ParcelStatus.IN_TRANSIT,
    "in transit": ParcelStatus.IN_TRANSIT,
    "on delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "out for delivery": ParcelStatus.OUT_FOR_DELIVERY,
    "delivered": ParcelStatus.DELIVERED,
}
_unmapped_statuses_logged: set[str] = set()
_unexpected_shapes_logged: set[str] = set()
_unparseable_timestamps_logged: set[str] = set()

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)


def _normalise_status(value: object) -> str | None:
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _warn_unmapped_status(value: object) -> None:
    text = str(value)
    if text in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(text)
    _LOGGER.warning(
        "Unrecognised GLS Canada status — help us map it. Open an issue and "
        "paste this line: %s\n  status=%r → reported as 'unknown'",
        _NEW_ISSUE_URL,
        value,
    )


def map_parcel_status_ca(value: object) -> ParcelStatus:
    """Map a Canadian status name to the canonical parcel status."""
    normalised = _normalise_status(value)
    if normalised is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(normalised)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(value)
    return ParcelStatus.UNKNOWN


def map_event_status_ca(value: object) -> ParcelStatus | None:
    """Map a Canadian activity status, keeping unknown history events neutral."""
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
    _LOGGER.warning("GLS Canada response has an unexpected field shape: %s", marker)


def _parse_timestamp(value: object) -> str | None:
    """Return a canonical ISO timestamp for GLS Canada's offset-free values.

    The carrier currently supplies ``YYYY-MM-DD HH:MM:SS`` without an offset.
    GLS's existing DE transport treats the same format as UTC so the shared
    retention and sorting helpers can operate on it; CA follows that established
    integration convention until the carrier publishes a timezone field.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).isoformat()
    except ValueError:
        if value not in _unparseable_timestamps_logged:
            _unparseable_timestamps_logged.add(value)
            _LOGGER.warning("GLS Canada returned an unparseable timestamp format")
        return None


def _shipment_for_code(raw: dict, parcel_no: str | None) -> dict | None:
    shipments = raw.get("shipments")
    if not isinstance(shipments, list):
        _warn_shape("shipments", shipments)
        return None
    target = (parcel_no or "").casefold()
    for shipment in shipments:
        if not isinstance(shipment, dict):
            _warn_shape("shipments[]", shipment)
            continue
        number = shipment.get("trackingNumber")
        if isinstance(number, str) and number.casefold() == target:
            return shipment
    return None


def _usable_body(body: dict | None, parcel_no: str) -> dict[str, Any] | None:
    """Return ``body`` only when it carries a real shipment for ``parcel_no``.

    ``NORECORD`` is GLS Canada's HTTP-200 "no such parcel" answer, not a
    transport error, so it collapses to ``None`` like a missing shipment does.
    """
    if body is None:
        return None
    shipment = _shipment_for_code(body, parcel_no)
    if shipment is None:
        _warn_shape("shipments.trackingNumber", None)
        return None
    current_status = shipment.get("currentStatus")
    if (
        isinstance(current_status, dict)
        and _normalise_status(current_status.get("name")) == "norecord"
    ):
        return None
    return body


async def _async_get_json(
    session: aiohttp.ClientSession, url: str
) -> tuple[int, dict | None]:
    async with session.get(url, headers={"Accept-Language": "en-CA"}) as response:
        status = response.status
        if status != 200:
            return status, None
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            _LOGGER.warning("GLS Canada returned an unparseable JSON body")
            return status, None
    if not isinstance(body, dict):
        _warn_shape("$", body)
        return status, None
    return status, body


async def async_get_parcel_ca(
    session: aiohttp.ClientSession, parcel_no: str, postal_code: str
) -> dict[str, Any] | None:
    """Fetch a CA shipment, preferring the postcode-enhanced public route."""
    normalized_postal_code = postal_code.replace(" ", "").upper()
    details_url = GLS_CA_TRACKING_DETAILS_URL.format(
        postal_code=normalized_postal_code, parcel_no=parcel_no
    )
    status, body = await _async_get_json(session, details_url)
    if status == 200:
        return _usable_body(body, parcel_no)

    # The postcode route is richer but GLS can reject it (for example when a
    # detail/POD lookup is unavailable to the current client). Its anonymous
    # code-only sibling remains usable in those cases. Do not fall back on
    # server failures: those must still surface through the coordinator.
    if status not in (401, 403, 404):
        raise GlsApiError(status)

    basic_url = GLS_CA_TRACKING_URL.format(parcel_no=parcel_no)
    status, body = await _async_get_json(session, basic_url)
    if status != 200:
        raise GlsApiError(status)
    return _usable_body(body, parcel_no)


def _dimensions(shipment: dict) -> dict | None:
    parcels = shipment.get("parcels")
    if not isinstance(parcels, list) or not parcels:
        return None
    dimensions = {
        (parcel.get("length"), parcel.get("width"), parcel.get("height"))
        for parcel in parcels
        if isinstance(parcel, dict)
    }
    if len(dimensions) != 1:
        return None
    length, width, height = dimensions.pop()
    if not any(value is not None for value in (length, width, height)):
        return None
    text = (
        f"{length} x {width} x {height} cm"
        if None not in (length, width, height)
        else None
    )
    return {"length": length, "width": width, "height": height, "text": text}


def _party_label(value: object) -> str | None:
    """Return the sender/consignee name, or its available location label."""
    if isinstance(value, str):
        return value or None
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("name"), str) and value["name"]:
        return value["name"]
    parts = [
        value.get("city"),
        value.get("provinceCode"),
        value.get("countryCode"),
    ]
    return ", ".join(str(part) for part in parts if part) or None


def _history(shipment: dict) -> list[dict]:
    events: list[tuple[datetime, dict]] = []
    for parcel in shipment.get("parcels") or []:
        if not isinstance(parcel, dict):
            continue
        for activity in parcel.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            timestamp = _parse_timestamp(activity.get("activityDate"))
            if timestamp is None:
                continue
            events.append(
                (
                    datetime.fromisoformat(timestamp),
                    {
                        "timestamp": timestamp,
                        "status": map_event_status_ca(activity.get("status")),
                        "raw_status": activity.get("status"),
                    },
                )
            )
    events.sort(key=lambda item: item[0])
    return [event for _, event in events][-HISTORY_MAX_EVENTS:]


def normalize_parcel_ca(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "CA",
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Normalize a GLS Canada response while preserving its full ``raw`` body."""
    shipment = _shipment_for_code(raw, parcel_no)
    if shipment is None:
        shipment = {}
    current_status = shipment.get("currentStatus")
    if not isinstance(current_status, dict):
        current_status = {}
    status_name = current_status.get("name")
    status = map_parcel_status_ca(status_name)
    delivered_at = _parse_timestamp(current_status.get("date"))
    barcode = shipment.get("trackingNumber") or parcel_no
    template = COUNTRIES.get(country, {}).get("tracking_url")
    url = template.format(parcel_no=barcode) if template and barcode else (
        TRACKING_URL.format(parcel_no=barcode) if barcode else None
    )

    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": _party_label(shipment.get("sender")),
        "receiver": _party_label(shipment.get("consignee")),
        "status": status,
        "raw_status": status_name,
        "delivered": status == ParcelStatus.DELIVERED,
        "delivered_at": delivered_at if status == ParcelStatus.DELIVERED else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": url,
        "weight": shipment.get("totalWeight"),
        "dimensions": _dimensions(shipment),
        "history": _history(shipment) if include_history else None,
        "raw": raw,
    }


__all__ = [
    "async_get_parcel_ca",
    "map_event_status_ca",
    "map_parcel_status_ca",
    "normalize_parcel_ca",
]
