"""GLS Netherlands: the keyless ``GET`` transport, payload mapping and status map.

Moved out of the former ``api.py``/``parcels.py`` as part of the
``countries/`` restructure (BUILD_PLAN_DE.md's "structural decision") — pure
relocation, no behaviour change. ``api.py`` and ``parcels.py`` now dispatch to
this module by ``CONF_COUNTRY``. Packaged as ``countries/nl/`` for symmetry
with ``countries/de/`` rather than because NL needs an extra submodule of its
own — unlike DE, NL has no lifecycle module (no ``session.py`` here).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    DEFAULT_COUNTRY,
    HISTORY_MAX_EVENTS,
    PARCEL_DETAILS_URL,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)
from ...timeutils import parse_iso as _parse_iso

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport — one keyless GET, no body
# ---------------------------------------------------------------------------


async def async_get_parcel_nl(
    session: aiohttp.ClientSession,
    host: str,
    culture: str,
    parcel_no: str,
    postal_code: str,
) -> dict[str, Any] | None:
    """Fetch one NL parcel's tracking details.

    Returns the parsed JSON dict for a known parcel, or ``None`` when the
    endpoint answers ``204 No Content`` (unknown or not-yet-scanned parcel).
    Any other non-2xx status raises :class:`~..const.GlsApiError`; network
    errors propagate as ``aiohttp.ClientError``.

    The response is served with a ``text/plain`` mimetype, so the body is
    parsed with ``json.loads`` rather than ``response.json()``.
    """
    url = PARCEL_DETAILS_URL.format(
        host=host,
        parcel_no=parcel_no,
        postal_code=postal_code.replace(" ", ""),
        culture=culture,
    )
    async with session.get(url) as response:
        if response.status == 204:
            return None
        if response.status != 200:
            raise GlsApiError(response.status)
        text = await response.text()

    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as err:
        _LOGGER.warning("GLS returned an unparseable body for %s: %s", parcel_no, err)
        return None


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

# GLS numeric ``state`` → canonical ParcelStatus. GLS uses the same code on
# the top-level parcel and on each history scan, so one map drives both.
_STATE_MAP: dict[int, ParcelStatus] = {
    0: ParcelStatus.REGISTERED,        # Aangekondigd bij GLS
    1: ParcelStatus.IN_TRANSIT,        # Pakket ontvangen door GLS
    2: ParcelStatus.IN_TRANSIT,        # Aangekomen op GLS depot
    3: ParcelStatus.OUT_FOR_DELIVERY,  # Onderweg - geladen voor aflevering
    4: ParcelStatus.DELIVERED,         # Afgeleverd
}

# Points at the pre-filled issue template rather than a blank form, so a
# user following this link from their log lands somewhere that already
# asks the right questions.
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# States we have already warned about, so each unmapped one is logged only
# once per HA session.
_unmapped_states_logged: set[int] = set()


def _warn_unmapped_state(state: int) -> None:
    """Log an unmapped GLS state once, with a copy-paste issue link."""
    if state in _unmapped_states_logged:
        return
    _unmapped_states_logged.add(state)
    _LOGGER.warning(
        "Unrecognised GLS state — help us map it. Open an issue and paste "
        "this line: %s\n  state=%s → reported as 'unknown'",
        _NEW_ISSUE_URL,
        state,
    )


def map_parcel_status_nl(state: int | None) -> ParcelStatus:
    """Map a GLS numeric ``state`` to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unmapped non-null state reports ``unknown`` with a one-shot warning.
    """
    if state is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATE_MAP.get(state)
    if mapped is not None:
        return mapped
    _warn_unmapped_state(state)
    return ParcelStatus.UNKNOWN


def map_event_status(state: int | None) -> ParcelStatus | None:
    """Map a history scan's ``state`` to a canonical status, or ``None``.

    Unmapped non-null states keep ``status: null`` on the history entry and
    warn once (reusing the parcel-state one-shot set).
    """
    if state is None:
        return None
    mapped = _STATE_MAP.get(state)
    if mapped is not None:
        return mapped
    _warn_unmapped_state(state)
    return None


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------


def build_history(
    scans: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the GLS ``scans`` array.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. GLS provides human event text, so ``raw_status`` is the
    Dutch ``eventReasonDescr``. Sorted oldest → newest and capped to the most
    recent ``max_events``. Comes free with the details call (no extra
    request), unlike DHL's separate track-trace call.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for scan in scans or []:
        timestamp = scan.get("dateTime")
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(scan.get("state")),
            "raw_status": scan.get("eventReasonDescr"),
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def _tracking_url(
    parcel_no: str | None, postal_code: str | None, country: str
) -> str | None:
    """Construct the consumer tracking deep-link for a parcel.

    Prefers the per-country deep-link (NL needs the postcode as well as the
    parcel number — the generic ``gls-group.com`` link intermittently returns
    "package not found" for NL parcels). Falls back to the generic link when
    the country has no specific template or the postcode is unknown.
    """
    if not parcel_no:
        return None
    template = COUNTRIES.get(country, {}).get("tracking_url")
    if template and postal_code:
        return template.format(parcel_no=parcel_no, postal_code=postal_code)
    return TRACKING_URL.format(parcel_no=parcel_no)


def _dimensions(raw: dict) -> dict | None:
    """Return the canonical dimensions dict (cm) from the raw payload.

    ``text`` is only formatted when all three sides are known — a partial
    payload must not yield strings like ``"30 x None x None cm"``. Mirrors
    DPD's ``_augment_dimensions`` behaviour.
    """
    length = raw.get("length")
    width = raw.get("width")
    height = raw.get("height")
    if not any(value for value in (length, width, height)):
        return None
    if length is None or width is None or height is None:
        text: str | None = None
    else:
        text = f"{length} x {width} x {height} cm"
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": text,
    }


def _pickup_point(raw: dict) -> str | None:
    """Return the ParcelShop name when the parcel is a pickup, else ``None``."""
    shop = (raw.get("deliveryScanInfo") or {}).get("parcelShop")
    if isinstance(shop, dict):
        return shop.get("name")
    if isinstance(shop, str):
        return shop or None
    return None


def normalize_parcel_nl(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = DEFAULT_COUNTRY,
    include_history: bool = False,
) -> dict:
    """Return a carrier-agnostic parcel dict with the original GLS payload under ``raw``.

    GLS provides more than DHL: ``weight`` and ``dimensions`` are populated.
    The expected delivery window is ``deliveryStatus.etaTimestampMin/Max``
    (only while the parcel is still on its way).

    ``history`` is the optional per-parcel status timeline — opt-in, default
    off (``None``), kept identical to the other suite carriers. GLS returns
    the timeline in the same call, so enabling it costs no extra request.
    """
    address = raw.get("addressInfo") or {}
    sender = (address.get("from") or {}).get("name")
    receiver = (address.get("to") or {}).get("name")

    scan_info = raw.get("deliveryScanInfo") or {}
    state = raw.get("state")
    delivered = bool(scan_info.get("isDelivered")) or state == 4

    delivery_status = raw.get("deliveryStatus") or {}
    eta_min = delivery_status.get("etaTimestampMin")
    eta_max = delivery_status.get("etaTimestampMax")

    parcels_list = raw.get("parcels") or []
    raw_status = parcels_list[0].get("lastStatus") if parcels_list else None

    is_pickup = bool(raw.get("isPickup")) or bool(
        (raw.get("deliveryListInfo") or {}).get("isParcelShop")
    )

    weight = raw.get("weighedWeight")
    if weight is None:
        weight = raw.get("suppliedWeight")

    return {
        "carrier": "GLS",
        "barcode": raw.get("parcelNo"),
        "sender": sender,
        "receiver": receiver or None,
        "status": map_parcel_status_nl(state),
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": scan_info.get("dateTime") if delivered else None,
        "planned_from": None if delivered else eta_min,
        "planned_to": None if delivered else eta_max,
        "pickup": is_pickup,
        "pickup_point": _pickup_point(raw) if is_pickup else None,
        "url": _tracking_url(raw.get("parcelNo"), postal_code, country),
        "weight": weight,
        "dimensions": _dimensions(raw),
        "history": build_history(raw.get("scans")) if include_history else None,
        "raw": raw,
    }


# NOTE: `sort_parcels_by_ts` and the delivered-retention filter are NOT here.
# They operate on already-normalized canonical dicts and are identical
# regardless of which country produced them, so they stayed in `parcels.py`
# as the generic (country-agnostic) part of the dispatcher.
