"""Shared coordinator machinery for the GLS parcel tracker integration.

Dynamic polling, change events and the active/delivered split live here; each
source (``tracking/`` and ``account/``) subclasses it and only supplies the
fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INCLUDE_HISTORY,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .contents import ContentsStore, normalize_code
from .parcels import _apply_delivered_filter, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    return (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model.

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller).
    """
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours
    this natively). Otherwise, clamp the naive next-due time forward to the
    next anchor whenever it would land inside the quiet window — including
    when ``now`` itself is already inside it (an anchor poll computing its
    own follow-up).
    """
    if tier_minutes is None:
        return None

    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class GlsBaseCoordinator(DataUpdateCoordinator[list[dict]]):
    """Publishes the canonical parcel lists; subclasses supply the fetch.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # Recomputed at the end of every refresh — start with the hot
            # cadence so the very first poll, right after setup, happens
            # promptly regardless of what it finds.
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self.contents_store = ContentsStore(hass, entry.entry_id)
        # Every code this entry lists, delivered ones past the retention filter
        # included: update_contents asks which entry a code belongs to.
        self.known_codes: frozenset[str] = frozenset()
        self.delivered: list[dict] = []
        # parcel_no values confirmed delivered on a prior refresh — excluded
        # from the fetch this cycle since a delivered parcel's payload can
        # never change again. Lives for the integration's lifetime (resets on
        # restart).
        self._delivered_codes: set[str] = set()
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this GLS device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # Tier last computed by _hottest_tier_minutes — surfaced in
        # diagnostics. None before the first refresh, and whenever polling is
        # fully suspended (nothing tracked, or everything delivered).
        self._current_tier_minutes: int | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Parcel numbers currently skipped from the fetch (diagnostics only).

        Always empty for an account entry, whose feed is read whole every poll.
        """
        return self._delivered_codes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered list per the configured retention option."""
        return _apply_delivered_filter(parcels, self.config_entry)

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self._async_poll()
        except Exception:
            # The base class keeps the last interval after a failure, which
            # after an overnight poll is the whole quiet window.
            now = dt_util.now()
            tier = _hottest_tier_minutes(self.data or [], now)
            self.update_interval = _next_update_interval(
                now,
                tier if tier is not None else MID_INTERVAL_MINUTES,
                self.config_entry.entry_id,
            )
            raise

    async def _async_poll(self) -> list[dict]:
        raise NotImplementedError

    def _finish_update(
        self,
        entries: list[tuple[str, dict]],
        *,
        successful: bool,
        discovery_minutes: int | None = None,
    ) -> list[dict]:
        """Publish a complete normalized snapshot using the shared parcel lifecycle."""
        for code, parcel in entries:
            user_value = self.contents_store.get(code)
            if user_value is not None:
                parcel["contents"] = user_value
        self.known_codes = frozenset(
            normalize_code(value)
            for code, parcel in entries
            for value in (code, parcel.get("barcode"))
            if value
        )
        active = [p for _, p in entries if not p["delivered"]]
        delivered = [p for _, p in entries if p["delivered"]]

        delivered_sorted = sort_parcels_by_ts(
            delivered, "delivered_at", descending=True
        )
        self.delivered = self._apply_delivered_filter(delivered_sorted)
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Events and remembered state cover every parcel; the retention option
        # only limits what the delivered sensor shows.
        incoming = normalized_active + delivered_sorted
        self._fire_change_events(incoming)
        self._known_state = {
            p["barcode"]: p["status"] for p in incoming if p.get("barcode")
        }
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in incoming
            if p.get("barcode")
        }

        # Only stamp the diagnostic timestamp when at least one fetch actually
        # succeeded (or nothing needed fetching) — a poll that was served
        # entirely from cache must not present itself as a successful update.
        if successful:
            self.last_success_time = datetime.now(UTC)

        now = dt_util.now()
        self._current_tier_minutes = (
            _hottest_tier_minutes(normalized_active, now) or discovery_minutes
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )

        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new vs. already present before HA started. Mirrors the other
        suite carriers, including the ``device_id`` on every payload and the
        ``value → null`` ETA transitions staying intentionally silent. The
        parcels span active + delivered, so the terminal hop is visible: a
        change **to** ``DELIVERED`` fires only ``gls_parcel_delivered``
        (never also ``_status_changed``), a barcode first seen
        already-delivered fires nothing, and ``registered`` only fires for
        not-yet-delivered new barcodes.
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
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
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
