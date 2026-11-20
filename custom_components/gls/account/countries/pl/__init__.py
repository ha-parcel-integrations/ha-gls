"""Canonical parcels from the Polish myGLS account feed.

The paged feed carries each parcel's machine status (``progressBarIdent``) and
the user-facing numbers; the per-parcel details call adds weight and the
Polish event history. Both status vocabularies are the ones the manual Polish
transport already maps, so this module reuses ``map_parcel_status_pl`` /
``map_event_status_pl`` rather than duplicating them — and, like that
transport, an unrecognised shipment code falls back to the newest event's
status. ``pickup_point`` stays ``None``: the details payload's ``parcelShop``
has not been seen populated on a real account parcel, so its shape is unknown.

The locker ``pin`` and the courier's phone number never reach the published
``raw`` attribute, the sensor state or the bus events — only an allowlist of
safe fields does.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import math
import re
from typing import Any
from urllib.parse import quote

from ....const import COUNTRIES, HISTORY_MAX_EVENTS, ParcelStatus
from ....timeutils import parse_iso
from ....tracking.countries.pl import map_event_status_pl, map_parcel_status_pl
from ...errors import GlsAccountFeedError
from ...feed import complete_items
from ...normalize import pick, text as _text
from .session import DETAILS_KEY

_LOGGER = logging.getLogger(__name__)
_TRACKING_URL = COUNTRIES["PL"]["tracking_url"]

# Safe, scalar feed/details fields that may enter the published ``raw``. Every
# one was seen on a real captured feed row / details payload (2026-11-17); the
# locker ``pin``, ``courierPhoneNumber``, URLs and every identity UUID are
# deliberately excluded. Add a field here only after seeing it on the wire.
_RAW_FEED_FIELDS = frozenset(
    {
        "shipmentNo",
        "trackingId",
        "stateDate",
        "packageAmount",
        "progressBar",
        "progressBarIdent",
        "receiverPostalCode",
        "senderName",
        "receiverName",
        "customName",
        "isManual",
        "deliveryCountryCode",
    }
)
_RAW_DETAILS_FIELDS = frozenset(
    {"weight", "reference", "deliveryMethod", "deliveryCountryCode"}
)


_PHONE_SEPARATORS = re.compile(r"[\s\-().]")
_COUNTRY_PREFIXES = ("+48", "0048")


def normalize_phone_pl(value: str) -> str | None:
    """Return the 9-digit dialling form ``SelfAsserted`` expects, or ``None``.

    Spaces, dashes and a ``+48``/``0048`` country prefix are what people
    naturally type; the stored phone must be the bare national number, never
    the token's ``+48…`` claim.
    """
    number = _PHONE_SEPARATORS.sub("", value)
    for prefix in _COUNTRY_PREFIXES:
        if number.startswith(prefix):
            number = number[len(prefix) :]
            break
    return (
        number if len(number) == 9 and number.isascii() and number.isdigit() else None
    )


def parse_feed_pl(feed: Any, uid: str | None) -> list[tuple[str, dict]]:
    """Return ``(shipmentNo, raw)`` pairs from a complete feed — or raise.

    Unlike Denmark's feed, a Polish row carries no owner field to compare with
    the signed-in identity; the feed endpoint is scoped to the bearer token, so
    ownership is enforced by the backend, not re-checked here. Completeness is
    checked against ``totalElements``, so a partial page can never replace the
    snapshot. The feed is shipment-level (one row per shipment, multi-collo
    included), so a repeated ``shipmentNo`` is a backend quirk: the first row
    wins and the hub keeps publishing, rather than one stray row failing the
    whole account.
    """
    entries: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for item in complete_items(feed, "totalElements"):
        number = item.get("shipmentNo")
        if not isinstance(number, str) or not number.strip():
            raise GlsAccountFeedError("GLS account parcel number could not be verified")
        number = number.strip()
        if number in seen:
            _LOGGER.debug("GLS Poland feed repeated a shipment; keeping the first row")
            continue
        seen.add(number)
        entries.append((number, item))
    return entries


def _details(raw: dict) -> dict:
    """Return the details payload the session attached to a feed row, or ``{}``."""
    details = raw.get(DETAILS_KEY)
    return details if isinstance(details, dict) else {}


def _shipment(raw: dict) -> dict:
    """Return the details payload's ``trackingShipment``, or ``{}``."""
    shipment = _details(raw).get("trackingShipment")
    return shipment if isinstance(shipment, dict) else {}


def _safe_raw(raw: dict) -> dict:
    """Drop access material before the payload can enter attributes or events."""
    return {
        **pick(raw, _RAW_FEED_FIELDS),
        **pick(_shipment(raw), _RAW_DETAILS_FIELDS),
    }


def _package_events(raw: dict) -> list[dict]:
    """Flatten every package's status events from the details payload."""
    packages = _details(raw).get("trackingShipmentPackages")
    if not isinstance(packages, list):
        return []
    out: list[dict] = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        statuses = package.get("packageStatuses")
        if isinstance(statuses, list):
            out.extend(status for status in statuses if isinstance(status, dict))
    return out


def _weight(raw: dict) -> float | int | None:
    """Return the parcel weight from the details payload, if sane."""
    value = _shipment(raw).get("weight")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return value


def normalize_parcel_pl_account(raw: dict, *, include_history: bool = False) -> dict:
    """Normalize an account parcel, publishing only a credential-free raw copy."""
    safe_raw = _safe_raw(raw)
    statuses = [
        (parse_iso(event.get("packageStatusDate")), event)
        for event in _package_events(raw)
    ]
    statuses.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC))

    status = map_parcel_status_pl(raw.get("progressBarIdent"))
    if status == ParcelStatus.UNKNOWN:
        # Same rule as the manual Polish transport (whose warning promises it):
        # an unrecognised shipment code takes the status of the newest dated
        # event whose wording is mapped.
        status = next(
            (
                mapped
                for moment, event in reversed(statuses)
                if moment is not None
                and (mapped := map_event_status_pl(event.get("packageStatusName")))
                is not None
            ),
            ParcelStatus.UNKNOWN,
        )
    delivered = status == ParcelStatus.DELIVERED
    pickup = status == ParcelStatus.AT_PICKUP_POINT

    # Only a delivered parcel has a delivery moment (the suite's shape). The
    # newest "delivered" event gives it; without details — or before any such
    # event — the feed's ``stateDate`` stands in, as on the manual route.
    delivered_moment = (
        next(
            (
                moment
                for moment, event in reversed(statuses)
                if moment is not None
                and map_event_status_pl(event.get("packageStatusName"))
                == ParcelStatus.DELIVERED
            ),
            None,
        )
        or parse_iso(raw.get("stateDate"))
        if delivered
        else None
    )
    history = None
    if include_history:
        history = [
            {
                "timestamp": moment.isoformat(),
                "status": map_event_status_pl(event.get("packageStatusName")),
                "raw_status": _text(event.get("packageStatusName")),
                "location": _text(event.get("packageStatusPlace")),
            }
            for moment, event in statuses
            if moment is not None
        ][-HISTORY_MAX_EVENTS:]

    barcode = _text(raw.get("shipmentNo"))
    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": _text(raw.get("senderName")),
        "receiver": _text(raw.get("receiverName")),
        "status": status,
        "raw_status": _text(raw.get("progressBar")),
        "delivered": delivered,
        "delivered_at": delivered_moment.isoformat() if delivered_moment else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": None,
        "url": _TRACKING_URL.format(parcel_no=quote(barcode, safe=""))
        if barcode
        else None,
        "weight": _weight(raw),
        "dimensions": None,
        "history": history,
        "raw": safe_raw,
    }
