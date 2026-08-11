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
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DE_PARCEL_NUMBER,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    ParcelStatus,
)
from .countries.de import _known_parcel_numbers as _de_known_parcel_numbers
from .countries.de.session import GlsDeSession, GlsDeSessionError
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
        self,
        hass: HomeAssistant,
        client: GlsApiClient,
        entry: ConfigEntry,
        *,
        de_session: GlsDeSession | None = None,
    ) -> None:
        """Initialise the coordinator.

        ``de_session`` is only set for a DE hub (``__init__.py`` constructs
        one alongside the DE-aware ``client``) — every DE-specific branch
        below is gated on it being not ``None``, so an NL hub's behaviour is
        completely untouched.
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self._de_session = de_session
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

    def _prepare_de_poll(self, tracked: list[dict]) -> None:
        """Seed the DE transport's per-process ``parcelNumber`` cache before polling.

        Without this, a fresh process (HA restart) has no way to resolve an
        already-tracked DE parcel's ``parcelNumber`` from a bare ``409`` —
        see ``countries/de/__init__.py``'s module-cache note. This only
        *reads* the persisted value and seeds the transport's own cache
        (``setdefault``, never clobbering an in-process value already
        learned this session); the cache itself is still owned and defined
        by ``countries/de/__init__.py``.
        """
        for item in tracked:
            tracking_ref = item.get(CONF_PARCEL_NO)
            parcel_number = item.get(CONF_DE_PARCEL_NUMBER)
            if tracking_ref and parcel_number:
                _de_known_parcel_numbers.setdefault(tracking_ref, parcel_number)

    def _persist_de_state(self, tracked: list[dict]) -> None:
        """Persist a newly-learned ``parcelNumber``, and recover from a session reregister.

        Runs after the poll, so ``self._raw_cache`` already reflects
        anything freshly fetched this round. A change here goes through
        ``async_update_entry``, which — once the entry's update listener is
        registered (i.e. after the very first refresh) — triggers one extra
        refresh, same as any other option edit; not during first setup,
        where the listener isn't registered yet.
        """
        assert self._de_session is not None
        new_tracked = list(tracked)
        parcels_changed = False
        for index, item in enumerate(new_tracked):
            tracking_ref = item.get(CONF_PARCEL_NO)
            raw = self._raw_cache.get(tracking_ref) if tracking_ref else None
            parcel_number = raw.get("parcelNumber") if raw else None
            if parcel_number and item.get(CONF_DE_PARCEL_NUMBER) != parcel_number:
                new_tracked[index] = {**item, CONF_DE_PARCEL_NUMBER: parcel_number}
                parcels_changed = True

        reregistered = self._de_session.pop_reregistered()
        if reregistered:
            # The carrier-side tracked list was reset under a new
            # appInstanceId — every previously learned parcelNumber is
            # stale until re-POSTed, so drop it from both the in-process
            # transport cache and the persisted state.
            for item in new_tracked:
                tracking_ref = item.get(CONF_PARCEL_NO)
                if tracking_ref:
                    _de_known_parcel_numbers.pop(tracking_ref, None)
            new_tracked = [
                {k: v for k, v in item.items() if k != CONF_DE_PARCEL_NUMBER}
                for item in new_tracked
            ]
            parcels_changed = True
            _LOGGER.warning(
                "GLS DE app instance was reregistered — every tracked "
                "parcel will be re-added on the next poll."
            )

        new_data = dict(self.config_entry.data)
        data_changed = (
            new_data.get(CONF_DE_APP_INSTANCE_ID) != self._de_session.app_instance_id
        )
        if data_changed:
            new_data[CONF_DE_APP_INSTANCE_ID] = self._de_session.app_instance_id

        if parcels_changed or data_changed:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
                options={**self.config_entry.options, CONF_PARCELS: new_tracked},
            )

    async def _async_update_data(self) -> list[dict]:
        tracked = self._tracked()
        country = self.config_entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
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

        if country == "DE" and self._de_session is not None:
            self._prepare_de_poll(tracked)

        results = await asyncio.gather(
            *(
                self._client.async_get_parcel(parcel_no, postal_code)
                for parcel_no, postal_code in pairs
            ),
            return_exceptions=True,
        )

        # Each raw payload is kept with its postcode so the tracking deep-link
        # (NL needs the postcode) can be built per parcel.
        raws: list[tuple[dict, str]] = []
        errors = 0
        for (parcel_no, postal_code), result in zip(pairs, results):
            if isinstance(result, BaseException):
                if isinstance(result, GlsDeSessionError):
                    # A token-refresh/identity failure is not one parcel's
                    # problem — the whole poll can't authenticate, so fail it
                    # outright rather than caching a per-parcel error.
                    raise UpdateFailed(f"GLS DE session error: {result}") from result
                if not isinstance(result, (GlsApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                _LOGGER.warning("GLS fetch failed for %s: %s", parcel_no, result)
                cached = self._raw_cache.get(parcel_no)
                if cached is not None:
                    raws.append((cached, postal_code))
                continue

            if result is None:
                # 204 — unknown or not yet scanned. Keep prior data if we have
                # it, otherwise show a pending placeholder so the user still
                # sees the tracked parcel.
                raws.append(
                    (
                        self._raw_cache.get(parcel_no)
                        or {"parcelNo": parcel_no, "state": None},
                        postal_code,
                    )
                )
                continue

            self._raw_cache[parcel_no] = result
            raws.append((result, postal_code))

        if pairs and errors == len(pairs) and not raws:
            raise UpdateFailed("GLS unreachable for all tracked parcels")

        if country == "DE" and self._de_session is not None:
            self._persist_de_state(tracked)

        include_history = self._include_history
        normalized = [
            normalize_parcel(
                raw,
                postal_code=postal_code,
                country=country,
                include_history=include_history,
            )
            for raw, postal_code in raws
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
