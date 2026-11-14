"""Canonical parcels from the Danish account feed."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import math
from typing import Any
from urllib.parse import quote

from ...const import HISTORY_MAX_EVENTS, TRACKING_URL, ParcelStatus
from ...timeutils import parse_iso

_LOGGER = logging.getLogger(__name__)
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)
_unmapped_states_logged: set[tuple[int | None, int | None]] = set()
_STATUS_MAP = {
    10: ParcelStatus.REGISTERED,
    30: ParcelStatus.IN_TRANSIT,
    35: ParcelStatus.IN_TRANSIT,
    50: ParcelStatus.DELIVERED,
    60: ParcelStatus.AT_PICKUP_POINT,
    70: ParcelStatus.DELIVERED,
    80: ParcelStatus.PROBLEM,
    100: ParcelStatus.RETURNING,
}
# These fields contain the useful parcel data, without access codes, identity
# credentials, signed/share links, images or unexpected nested extensions.
_RAW_FIELDS = frozenset(
    {
        "parcelNumber",
        "senderName",
        "senderZipCode",
        "receiverName",
        "receiverAddress",
        "receiverZipCode",
        "receiverCity",
        "actualWeight",
        "parcelDimensions",
        "parcelShopId",
        "parcelShopNumber",
        "scheduledReturnDate",
        "parcelServiceType",
        "parcelType",
        "isDelivered",
        "isArchived",
        "isManuelAdded",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "location",
        "eventDateTime",
        "eventReasonCombination",
        "isFinal",
        "parcelState",
        "parcelStateText",
    }
)


def _scalar(value: Any) -> bool:
    """Known fields may carry scalars, never unreviewed nested credentials."""
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_raw(raw: dict) -> dict:
    """Drop access material before the payload can enter attributes or events."""
    result = {
        key: value
        for key, value in raw.items()
        if key in _RAW_FIELDS and _scalar(value)
    }
    events = raw.get("parcelStatusDtoList")
    if isinstance(events, list):
        result["parcelStatusDtoList"] = [
            {
                key: value
                for key, value in event.items()
                if key in _EVENT_FIELDS and _scalar(value)
            }
            for event in events
            if isinstance(event, dict)
        ]
    return result


def _code(value: Any) -> int | None:
    """Only integer machine codes are suitable for status mapping or logs."""
    return value if type(value) is int else None


def map_event_status_dk(event: dict) -> ParcelStatus | None:
    """Map observed machine states; retain unknown scans as neutral history."""
    state = _code(event.get("parcelState"))
    reason = _code(event.get("eventReasonCombination"))
    status = _STATUS_MAP.get(state) if state is not None else None
    if state == 40:
        if reason == 221:
            status = ParcelStatus.OUT_FOR_DELIVERY
        elif reason == 1586:
            status = ParcelStatus.IN_TRANSIT
    if status is None and (state, reason) not in _unmapped_states_logged:
        _unmapped_states_logged.add((state, reason))
        _LOGGER.warning(
            "Unrecognised GLS Denmark event codes state=%s reason=%s; please report at %s",
            state,
            reason,
            _NEW_ISSUE_URL,
        )
    return status


def _text(value: Any) -> str | None:
    """Accept only a nonempty human-readable scalar string."""
    return value.strip() or None if isinstance(value, str) else None


def _events(raw: dict) -> list[tuple[datetime | None, dict]]:
    """Sort events by their own timestamps, keeping ties in carrier order."""
    events = raw.get("parcelStatusDtoList")
    if not isinstance(events, list):
        return []
    parsed = [
        (parse_iso(event.get("eventDateTime")), event)
        for event in events
        if isinstance(event, dict)
    ]
    return sorted(
        parsed,
        key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def normalize_parcel_dk(raw: dict, *, include_history: bool = False) -> dict:
    """Normalize an account parcel, publishing only a credential-free raw copy."""
    safe_raw = _safe_raw(raw)
    events = _events(safe_raw)
    mapped = [(moment, event, map_event_status_dk(event)) for moment, event in events]
    status = next(
        (state for _, _, state in mapped if state is not None), ParcelStatus.UNKNOWN
    )
    # An unrecognised later scan could reopen a delivered shipment. An old
    # delivery scan alone cannot prove its current completion in that case.
    if mapped and mapped[0][2] is None and status == ParcelStatus.DELIVERED:
        status = ParcelStatus.UNKNOWN
    if raw.get("isDelivered") is True:
        status = ParcelStatus.DELIVERED
    delivered = status == ParcelStatus.DELIVERED
    latest = events[0][1] if events else {}
    delivered_moment = (
        next(
            (
                moment
                for moment, _, state in mapped
                if moment is not None and state == ParcelStatus.DELIVERED
            ),
            None,
        )
        if delivered
        else None
    )
    pickup = any(
        state == ParcelStatus.AT_PICKUP_POINT
        or (
            event.get("parcelState") == 40
            and event.get("eventReasonCombination") == 1586
        )
        for _, event, state in mapped
    )
    pickup_point = None
    if pickup:
        pickup_point = ", ".join(
            value
            for key in ("receiverAddress", "receiverZipCode", "receiverCity")
            if (value := _text(safe_raw.get(key)))
        ) or _text(safe_raw.get("parcelShopNumber"))
    barcode = _text(safe_raw.get("parcelNumber"))
    history = None
    if include_history:
        history = [
            {
                "timestamp": moment.isoformat(),
                "status": state,
                "raw_status": _text(event.get("parcelStateText")),
            }
            for moment, event, state in sorted(
                mapped, key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC)
            )
            if moment is not None
        ][-HISTORY_MAX_EVENTS:]
    weight = safe_raw.get("actualWeight")
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not math.isfinite(weight)
        or weight < 0
    ):
        weight = None
    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": _text(safe_raw.get("senderName")),
        "receiver": _text(safe_raw.get("receiverName")),
        "status": status,
        "raw_status": _text(latest.get("parcelStateText")),
        "delivered": delivered,
        "delivered_at": delivered_moment.isoformat() if delivered_moment else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": TRACKING_URL.format(parcel_no=quote(barcode, safe=""))
        if barcode
        else None,
        "weight": weight,
        "dimensions": None,
        "history": history,
        "raw": safe_raw,
    }
