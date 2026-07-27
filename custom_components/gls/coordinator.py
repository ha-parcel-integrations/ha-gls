"""Coordinator for the GLS parcel tracker integration."""
from __future__ import annotations

import asyncio
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
    ParcelStatus,
)
from .parcels import (
    _apply_delivered_filter,
    normalize_parcel,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


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

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered list per the configured retention option."""
        return _apply_delivered_filter(parcels, self.config_entry)

    async def _async_update_data(self) -> list[dict]:
        tracked = self._tracked()
        pairs = [
            (item[CONF_PARCEL_NO], item[CONF_POSTAL_CODE])
            for item in tracked
            if item.get(CONF_PARCEL_NO) and item.get(CONF_POSTAL_CODE)
        ]

        # Drop cache entries for parcels that were untracked, so the cache
        # stays bounded to what the user still follows.
        tracked_numbers = {parcel_no for parcel_no, _ in pairs}
        self._raw_cache = {
            k: v for k, v in self._raw_cache.items() if k in tracked_numbers
        }

        results = await asyncio.gather(
            *(
                self._client.async_get_parcel(parcel_no, postal_code)
                for parcel_no, postal_code in pairs
            ),
            return_exceptions=True,
        )

        raws: list[dict] = []
        errors = 0
        for (parcel_no, _), result in zip(pairs, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (GlsApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                _LOGGER.warning("GLS fetch failed for %s: %s", parcel_no, result)
                cached = self._raw_cache.get(parcel_no)
                if cached is not None:
                    raws.append(cached)
                continue

            if result is None:
                # 204 — unknown or not yet scanned. Keep prior data if we have
                # it, otherwise show a pending placeholder so the user still
                # sees the tracked parcel.
                raws.append(
                    self._raw_cache.get(parcel_no)
                    or {"parcelNo": parcel_no, "state": None}
                )
                continue

            self._raw_cache[parcel_no] = result
            raws.append(result)

        if pairs and errors == len(pairs) and not raws:
            raise UpdateFailed("GLS unreachable for all tracked parcels")

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [p for p in normalized if not p["delivered"]]
        delivered = [p for p in normalized if p["delivered"]]

        self.delivered = self._apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True)
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set (mirrors the other suite carriers).
        incoming = normalized_active + self.delivered
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
        # succeeded (or nothing is tracked) — a poll that was served entirely
        # from cache must not present itself as a successful update.
        if not pairs or errors < len(pairs):
            self.last_success_time = datetime.now(timezone.utc)
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
