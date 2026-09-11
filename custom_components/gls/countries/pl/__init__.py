"""GLS Poland: keyless national myGLS transport, mapping and status maps.

Poland runs its own national backend (`mygls.gls-poland.com.pl`), and its
public tracking route is the only GLS surface that gives a full event history
**without** a postcode. That is why PL is a transport of its own rather than
one more row on ``countries/group/``: the pan-EU leaf does resolve Polish
numbering, but it carries no history without the postcode-bearing
``rstt028`` call, dates its delivery from prose, and reports a ParcelShop
drop-off as a delivery — a real Polish parcel proved the national route
distinguishes the drop-off from the recipient collecting it hours later.

Two status levels, and they are not the same vocabulary:

* the **shipment** level is a machine code (``progressBarIdent``) shared with
  the pan-EU group leaves, so the map below is the group's, matched exactly;
* the **event** level has no code at all — only localized Polish text — so
  ``_EVENT_TEXT_MAP`` holds the values actually seen on the wire and nothing
  else. Unseen text stays neutral and logs once; translations are never
  guessed.

The endpoint takes the parcel number alone. The hub's postcode is still
required at setup (it is every GLS hub's key and the default for parcels added
later) and is simply never sent, exactly as ``countries/us/`` does it.
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    GLS_PL_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# ``progressBarIdent`` — the pan-EU group vocabulary. Only ``DELIVERED`` has
# been seen on a Polish parcel; the rest is the documented group catalogue,
# wired in ahead of a capture rather than guessed.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "PREADVICE": ParcelStatus.REGISTERED,
    "PROCESSING": ParcelStatus.REGISTERED,
    "INTRANSIT": ParcelStatus.IN_TRANSIT,
    "INWAREHOUSE": ParcelStatus.IN_TRANSIT,
    "INPICKUP": ParcelStatus.IN_TRANSIT,
    "MULTIPACK": ParcelStatus.IN_TRANSIT,
    "INDELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVEREDPS": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "NOTPICKEDUP": ParcelStatus.RETURNING,
    "RETURNED": ParcelStatus.RETURNING,
    "NOTDELIVERED": ParcelStatus.PROBLEM,
    "CANCELLED": ParcelStatus.PROBLEM,
    "CANCELED": ParcelStatus.PROBLEM,
    "UNAVAILABLE": ParcelStatus.PROBLEM,
}

# ``eventReasons[].packageStatusName``, captured from two real Polish parcels.
# Note that "niedoręczona" (not delivered) contains "doręczona" (delivered):
# these are matched exactly, never by substring.
_EVENT_TEXT_MAP: dict[str, ParcelStatus] = {
    "utworzenie paczki": ParcelStatus.REGISTERED,
    "w drodze": ParcelStatus.IN_TRANSIT,
    "w doręczeniu": ParcelStatus.OUT_FOR_DELIVERY,
    "gotowa do odbioru": ParcelStatus.AT_PICKUP_POINT,
    "niedoręczona": ParcelStatus.PROBLEM,
    "doręczona": ParcelStatus.DELIVERED,
}

_unmapped_statuses_logged: set[str] = set()
_unmapped_events_logged: set[str] = set()
_unexpected_keys_logged: set[str] = set()
_unparseable_timestamps_logged: set[str] = set()
_history_order_warned = False
_shop_delivery_warned = False

# Everything the two captured bodies carry. A new key is a payload change
# worth reporting, not a reason to fail.
_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "parcelNumber",
        "stateDate",
        "progressBar",
        "progressBarIdent",
        "hasDeliveryCode",
        "eventReasons",
    }
)

# The carrier's own semantic "no such parcel" answer: HTTP 400 with this code
# in the body. Distinguishing it from a moved route is what the research
# doc's control test is for, so an unexpected 4xx must not be folded in here.
_UNKNOWN_PARCEL_CODE = "mygls-tracking-400(111)"

_ACCEPT = "application/json"


def _normalise_text(value: object) -> str | None:
    """Casefold Polish status text, NFC-normalised so diacritics compare equal."""
    if not isinstance(value, str) or not value.strip():
        return None
    return unicodedata.normalize("NFC", value.strip()).casefold()


def map_parcel_status_pl(value: object) -> ParcelStatus:
    """Map ``progressBarIdent`` to the canonical parcel status."""
    if not isinstance(value, str) or not value.strip():
        return ParcelStatus.UNKNOWN
    ident = value.strip().upper()
    mapped = _STATUS_MAP.get(ident)
    if mapped is not None:
        return mapped
    if ident not in _unmapped_statuses_logged:
        _unmapped_statuses_logged.add(ident)
        _LOGGER.warning(
            "Unrecognised GLS Poland status — help us map it. Open an issue "
            "and paste this line: %s\n  progressBarIdent=%r → falling back to "
            "the newest event",
            _NEW_ISSUE_URL,
            value,
        )
    return ParcelStatus.UNKNOWN


def map_event_status_pl(value: object) -> ParcelStatus | None:
    """Map a Polish event name, keeping unseen wording neutral."""
    text = _normalise_text(value)
    if text is None:
        return None
    mapped = _EVENT_TEXT_MAP.get(text)
    if mapped is not None:
        return mapped
    if text not in _unmapped_events_logged:
        _unmapped_events_logged.add(text)
        _LOGGER.warning(
            "Unrecognised GLS Poland event text — help us map it. Open an "
            "issue and paste this line: %s\n  packageStatusName=%r → reported "
            "without a status",
            _NEW_ISSUE_URL,
            value,
        )
    return None


def _warn_unexpected_top_level_keys(raw: dict) -> None:
    """Report unknown top-level keys once, as ``name: type`` pairs only."""
    unknown = sorted(set(raw) - _KNOWN_TOP_LEVEL_KEYS)
    pairs = [f"{key}: {type(raw[key]).__name__}" for key in unknown]
    fresh = [pair for pair in pairs if pair not in _unexpected_keys_logged]
    if not fresh:
        return
    _unexpected_keys_logged.update(fresh)
    _LOGGER.warning(
        "GLS Poland returned unexpected top-level keys (names and types only, "
        "safe to paste into an issue at %s): %s",
        _NEW_ISSUE_URL,
        ", ".join(fresh),
    )


def _parse_datetime(value: object) -> datetime | None:
    """Parse a Polish timestamp, which carries a real UTC offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        if value not in _unparseable_timestamps_logged:
            _unparseable_timestamps_logged.add(value)
            _LOGGER.warning(
                "GLS Poland returned an unparseable timestamp format — please "
                "report it at %s",
                _NEW_ISSUE_URL,
            )
        return None


def _events(raw: dict) -> list[dict]:
    events = raw.get("eventReasons")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


async def async_get_parcel_pl(
    session: aiohttp.ClientSession, parcel_no: str, postal_code: str
) -> dict[str, Any] | None:
    """Fetch a PL parcel from the keyless national myGLS route.

    ``postal_code`` keeps every country's transport signature identical; the
    Polish lookup is keyed on the parcel number alone and never sends it.
    """
    del postal_code
    url = GLS_PL_TRACKING_URL.format(parcel_no=parcel_no)
    async with session.get(url, headers={"Accept": _ACCEPT}) as response:
        status = response.status
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            # A WAF/maintenance page answers HTML on a route that only ever
            # speaks JSON — never parse it as a parcel.
            _LOGGER.warning("GLS Poland returned a non-JSON body (HTTP %s)", status)
            return None
    if status == 400 and isinstance(body, dict) and body.get("code") == _UNKNOWN_PARCEL_CODE:
        # Semantic "unknown number", the carrier's own not-found answer.
        return None
    if status != 200:
        raise GlsApiError(status)
    if not isinstance(body, dict):
        _LOGGER.warning(
            "GLS Poland returned an unexpected top-level type: %s",
            type(body).__name__,
        )
        return None
    return body


def _history(events: list[dict]) -> list[dict]:
    """Build the canonical, oldest-first history from ``eventReasons[]``.

    The wire order is newest-first, which is asserted rather than trusted: the
    entries are sorted on their own timestamps and a body that was not already
    descending is reported once.
    """
    global _history_order_warned
    parsed: list[tuple[datetime, dict]] = []
    for event in events:
        timestamp = _parse_datetime(event.get("packageStatusDate"))
        if timestamp is None:
            continue
        parsed.append(
            (
                timestamp,
                {
                    "timestamp": timestamp.isoformat(),
                    "status": map_event_status_pl(event.get("packageStatusName")),
                    "raw_status": event.get("packageStatusName"),
                },
            )
        )
    timestamps = [item[0] for item in parsed]
    if not _history_order_warned and timestamps != sorted(timestamps, reverse=True):
        _history_order_warned = True
        _LOGGER.warning(
            "GLS Poland returned history events that are not newest-first — "
            "please report it at %s",
            _NEW_ISSUE_URL,
        )
    parsed.sort(key=lambda item: item[0])
    return [event for _, event in parsed][-HISTORY_MAX_EVENTS:]


def _warn_shop_delivery_once() -> None:
    """Report a drop-off reported as a delivery, the one open PL question.

    The national route's events distinguish "handed over at the GLS Point"
    from "dropped off at the GLS Point", but it has never been observed what
    ``progressBarIdent`` says *while* a parcel waits there — the only captured
    shop parcel had already been collected. If the shipment code claims
    ``DELIVERED`` while the newest event is the drop-off, that is the answer
    arriving, and it must be reported rather than silently corrected here.
    """
    global _shop_delivery_warned
    if _shop_delivery_warned:
        return
    _shop_delivery_warned = True
    _LOGGER.warning(
        "GLS Poland reported a parcel as delivered while its newest event is a "
        "GLS Point drop-off. Please report this at %s — it settles how "
        "progressBarIdent behaves for ParcelShop deliveries",
        _NEW_ISSUE_URL,
    )


def normalize_parcel_pl(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "PL",
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Normalize a GLS Poland response, preserving its full ``raw`` body."""
    _warn_unexpected_top_level_keys(raw)

    ident = raw.get("progressBarIdent")
    status = map_parcel_status_pl(ident)
    events = _events(raw)
    newest = events[0] if events else None
    newest_status = map_event_status_pl(newest.get("packageStatusName")) if newest else None
    if status == ParcelStatus.UNKNOWN and newest_status is not None:
        # The shipment code is unverified beyond DELIVERED; the event texts
        # are captured. Derive rather than report a known parcel as unknown.
        status = newest_status

    delivered = status == ParcelStatus.DELIVERED
    if delivered and newest_status == ParcelStatus.AT_PICKUP_POINT:
        _warn_shop_delivery_once()

    delivered_at = None
    if delivered:
        # ``stateDate`` tracks the newest event on every captured body; the
        # newest *parseable* event stands in if it is missing or malformed,
        # which is not necessarily the first entry.
        moment = _parse_datetime(raw.get("stateDate"))
        if moment is None:
            moments = [
                parsed
                for event in events
                if (parsed := _parse_datetime(event.get("packageStatusDate")))
            ]
            moment = max(moments) if moments else None
        delivered_at = moment.isoformat() if moment else None

    # The event description is the precise per-event wording ("delivered to
    # the GLS Point"); progressBar is only the progress bar's heading.
    event_text = newest.get("packageStatusDescription") if newest else None
    if isinstance(event_text, str):
        event_text = event_text.strip() or None
    raw_status = event_text or raw.get("progressBar")

    # The response echoes the carrier's canonical number, which carries one
    # more check digit than the short form users are given — both resolve on
    # this route, and the suite convention is to show the response's own.
    barcode = raw.get("parcelNumber") or parcel_no
    template = COUNTRIES.get(country, {}).get("tracking_url") or TRACKING_URL

    return {
        "carrier": "GLS",
        "barcode": barcode,
        # The public route carries no party or measurement fields at all —
        # those live behind the authenticated myGLS inbox, which this
        # integration deliberately does not use.
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status == ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": None,
        "url": template.format(parcel_no=barcode) if barcode else None,
        "weight": None,
        "dimensions": None,
        "history": _history(events) if include_history else None,
        "raw": raw,
    }


__all__ = [
    "async_get_parcel_pl",
    "map_event_status_pl",
    "map_parcel_status_pl",
    "normalize_parcel_pl",
]
