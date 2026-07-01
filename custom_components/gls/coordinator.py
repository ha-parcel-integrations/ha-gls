"""Coordinator for the GLS parcel tracker integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GlsApiClient, GlsApiError
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
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

_NEW_ISSUE_URL = "https://github.com/peternijssen/ha-gls/issues/new"

# States we have already warned about, so each unmapped one is logged only
# once per HA session.
_unmapped_states_logged: set[int] = set()


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


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
    """Return the canonical dimensions dict (cm) from the raw payload."""
    length = raw.get("length")
    width = raw.get("width")
    height = raw.get("height")
    if not any(value for value in (length, width, height)):
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{length} x {width} x {height} cm",
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


class GlsCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinator that polls each tracked GLS parcel on a fixed schedule.

    GLS has no account/feed, so the tracked parcels are the ``parcel_no`` +
    ``postal_code`` pairs the user entered (stored in the entry options). Each
    is fetched individually and merged into one list; ``coordinator.data`` is
    the active (not-yet-delivered) parcels, ``self.delivered`` the rest.
    """

    def __init__(
        self, hass: HomeAssistant, client: GlsApiClient, entry: ConfigEntry
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self.delivered: list[dict] = []
        # parcel_no -> last successful raw payload, so a transient fetch
        # failure or a 204 keeps the parcel visible instead of dropping its
        # sensor. Lives for the integration's lifetime (resets on restart).
        self._raw_cache: dict[str, dict] = {}
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this GLS device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[dict]:
        """Return the configured ``{parcel_no, postal_code}`` pairs."""
        return list(self.config_entry.options.get(CONF_PARCELS, []))

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    async def _async_update_data(self) -> list[dict]:
        tracked = self._tracked()
        raws: list[dict] = []
        errors = 0

        for item in tracked:
            parcel_no = item.get(CONF_PARCEL_NO)
            postal_code = item.get(CONF_POSTAL_CODE)
            if not parcel_no or not postal_code:
                continue
            try:
                raw = await self._client.async_get_parcel(parcel_no, postal_code)
            except (GlsApiError, aiohttp.ClientError) as err:
                errors += 1
                _LOGGER.warning("GLS fetch failed for %s: %s", parcel_no, err)
                cached = self._raw_cache.get(parcel_no)
                if cached is not None:
                    raws.append(cached)
                continue

            if raw is None:
                # 204 — unknown or not yet scanned. Keep prior data if we have
                # it, otherwise show a pending placeholder so the user still
                # sees the tracked parcel.
                raws.append(
                    self._raw_cache.get(parcel_no)
                    or {"parcelNo": parcel_no, "state": None}
                )
                continue

            self._raw_cache[parcel_no] = raw
            raws.append(raw)

        if tracked and errors == len(tracked) and not raws:
            raise UpdateFailed("GLS unreachable for all tracked parcels")

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [p for p in normalized if not p["delivered"]]
        delivered = [p for p in normalized if p["delivered"]]

        self.delivered = sort_parcels_by_ts(
            delivered, "delivered_at", descending=True
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        self._fire_change_events(normalized_active)
        self._known_state = {
            p["barcode"]: p["status"] for p in normalized_active if p.get("barcode")
        }
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in normalized_active
            if p.get("barcode")
        }

        self.last_success_time = datetime.now(timezone.utc)
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivery-time-changed events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new vs. already present before HA started. Mirrors the other
        suite carriers, including the ``device_id`` on every payload and the
        ``value → null`` ETA transitions staying intentionally silent.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_registered",
                    {**parcel, "device_id": device_id},
                )
                continue

            if self._known_state[barcode] != new_status:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": self._known_state[barcode],
                        "new_status": new_status,
                    },
                )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
