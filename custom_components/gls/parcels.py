"""Pure parcel mapping, normalization and list helpers for GLS.

No I/O and no Home Assistant objects beyond the config entry's options: this is
the carrier-specific status mapping and canonical-shape logic, kept apart from
the coordinator (fetching, caching, events) so it stays trivially unit-testable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

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


def map_parcel_status(state: int | None) -> ParcelStatus:
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


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values (GLS scan timestamps carry no timezone) are treated as UTC
    so a list always sorts without crashing on a mixed set.
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


def _tracking_url(parcel_no: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not parcel_no:
        return None
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


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
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
        "status": map_parcel_status(state),
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": scan_info.get("dateTime") if delivered else None,
        "planned_from": None if delivered else eta_min,
        "planned_to": None if delivered else eta_max,
        "pickup": is_pickup,
        "pickup_point": _pickup_point(raw) if is_pickup else None,
        "url": _tracking_url(raw.get("parcelNo")),
        "weight": weight,
        "dimensions": _dimensions(raw),
        "history": build_history(raw.get("scans")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalized parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        dt = _parse_iso(parcel.get(key_field))
        if dt is None:
            without_ts.append(parcel)
        else:
            with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


def _apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the configured retention option.

    ``parcels`` is already sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept); the
    ``parcels`` type keeps the N most recent. The parcels stay *tracked*
    either way — this only controls what the delivered sensor shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            p
            for p in parcels
            if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
        ]
    return parcels[:amount]
