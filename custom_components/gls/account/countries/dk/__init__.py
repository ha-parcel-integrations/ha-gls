"""Canonical parcels from the Danish account feed."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import math
from typing import Any
from urllib.parse import quote

from ....const import HISTORY_MAX_EVENTS, TRACKING_URL, ParcelStatus
from ....delivery_window import is_pickup
from ....timeutils import country_zone, localize_naive
from ...errors import GlsAccountFeedError
from ...feed import complete_items
from ...normalize import pick, text as _text

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


def _safe_raw(raw: dict) -> dict:
    """Drop access material before the payload can enter attributes or events."""
    result = pick(raw, _RAW_FIELDS)
    events = raw.get("parcelStatusDtoList")
    if isinstance(events, list):
        result["parcelStatusDtoList"] = [
            pick(event, _EVENT_FIELDS) for event in events if isinstance(event, dict)
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


def _moment(value: Any, zone: Any) -> datetime | None:
    """Parse an event time; one without an offset is Danish wall-clock time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return localize_naive(parsed, zone)


def _events(raw: dict) -> list[tuple[datetime | None, dict]]:
    """Sort events by their own timestamps, keeping ties in carrier order."""
    events = raw.get("parcelStatusDtoList")
    if not isinstance(events, list):
        return []
    zone = country_zone("DK")
    parsed = [
        (_moment(event.get("eventDateTime"), zone), event)
        for event in events
        if isinstance(event, dict)
    ]
    return sorted(
        parsed,
        key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def normalize_email_dk(value: str) -> str | None:
    """Return the sign-in email as typed, minus surrounding whitespace."""
    return value.strip() or None


def parse_feed_dk(feed: Any, uid: str | None) -> list[tuple[str, dict]]:
    """Return ``(number, raw)`` pairs from a complete, owned feed — or raise.

    ``totalCount`` must equal the items actually returned, and every item must
    belong to the signed-in identity: a partial or foreign list is rejected
    before anything is published, so it can never become an empty account.
    """
    entries: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for item in complete_items(feed, "totalCount"):
        number = item.get("parcelNumber")
        if (
            not isinstance(number, str)
            or not number.strip()
            or number.strip() in seen
            or item.get("userId") != uid
        ):
            raise GlsAccountFeedError(
                "GLS account parcel identity could not be verified"
            )
        seen.add(number.strip())
        entries.append((number.strip(), item))
    return entries


def _has_shop_id(value: Any) -> bool:
    """A positive id names a shop; 0 or less is what a home delivery may send."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    text = _text(value)
    if text is None:
        return False
    return not (text.lstrip("-").isdigit() and int(text) <= 0)


def normalize_parcel_dk(raw: dict, *, include_history: bool = False) -> dict:
    """Normalize an account parcel, publishing only a credential-free raw copy."""
    safe_raw = _safe_raw(raw)
    events = _events(safe_raw)
    mapped = [(moment, event, map_event_status_dk(event)) for moment, event in events]
    status = next(
        (state for _, _, state in mapped if state is not None), ParcelStatus.UNKNOWN
    )
    # A return is only delivered once it is back at the sender; nothing here
    # shows that, so isDelivered does not override it.
    if raw.get("isDelivered") is True and status is not ParcelStatus.RETURNING:
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
    shop_number = _text(safe_raw.get("parcelShopNumber"))
    # The shop fields name the route's end, so they count before any arrival.
    route_flag = (
        shop_number is not None
        or _has_shop_id(safe_raw.get("parcelShopId"))
        or any(
            event.get("parcelState") == 40
            and event.get("eventReasonCombination") == 1586
            for _, event, _ in mapped
        )
    )
    pickup = is_pickup(route_flag, status, (state for _, _, state in mapped))
    pickup_point = shop_number if pickup else None
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
        "contents": None,
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
