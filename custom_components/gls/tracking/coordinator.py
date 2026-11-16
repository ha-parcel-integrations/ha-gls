"""Coordinator for the tracking source.

Each tracking number the user entered is fetched individually and merged into
one list; the shared publishing lives in :mod:`..coordinator`.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DE_PARCEL_NUMBER,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DEFAULT_COUNTRY,
)
from ..coordinator import GlsBaseCoordinator
from .api import GlsApiClient, GlsApiError
from .countries.de import _known_parcel_numbers as _de_known_parcel_numbers
from .countries.de.session import GlsDeSession, GlsDeSessionError
from .parcels import normalize_parcel

_LOGGER = logging.getLogger(__name__)


class GlsCoordinator(GlsBaseCoordinator):
    """Coordinator that polls each tracked GLS parcel on a status-driven schedule.

    Tracked parcels are the ``parcel_no`` + ``postal_code`` pairs the user
    entered (stored in the entry options). Each is fetched individually and
    merged into one list; ``coordinator.data`` is the active (not-yet-delivered)
    parcels, ``self.delivered`` the rest.
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
        super().__init__(hass, entry)
        self._client = client
        self._de_session = de_session
        # parcel_no -> last successful raw payload, so a transient fetch
        # failure or a 204 keeps the parcel visible instead of dropping its
        # sensor. Lives for the integration's lifetime (resets on restart).
        self._raw_cache: dict[str, dict] = {}

    def _tracked(self) -> list[dict]:
        """Return the configured parcel numbers for this hub."""
        return list(self.config_entry.options.get(CONF_PARCELS, []))

    def _prepare_de_poll(self, tracked: list[dict]) -> None:
        """Seed the DE transport's per-process ``parcelNumber`` cache before polling.

        Without this, a fresh process (HA restart) has no way to resolve an
        already-tracked DE parcel's ``parcelNumber`` from a bare ``409`` —
        see ``tracking/countries/de/__init__.py``'s module-cache note. This only
        *reads* the persisted value and seeds the transport's own cache
        (``setdefault``, never clobbering an in-process value already
        learned this session); the cache itself is still owned and defined
        by ``tracking/countries/de/__init__.py``.
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
        where the listener isn't registered yet. An ``appInstanceId``-only
        write changes no option, so that one stays silent.
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

    async def _async_poll(self) -> list[dict]:
        tracked = self._tracked()
        country = self.config_entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        postal_code = self.config_entry.options.get(CONF_POSTAL_CODE)
        pairs: list[tuple[str, str]] = []
        for item in tracked:
            # The item fallback only supports a legacy entry before its setup
            # migration; all newly stored parcel items contain just the code.
            item_postal_code = postal_code or item.get(CONF_POSTAL_CODE)
            if item.get(CONF_PARCEL_NO) and item_postal_code:
                pairs.append((item[CONF_PARCEL_NO], item_postal_code))

        # Drop cache entries for parcels that were untracked, so the cache
        # stays bounded to what the user still follows.
        tracked_numbers = {parcel_no for parcel_no, _ in pairs}
        self._raw_cache = {
            k: v for k, v in self._raw_cache.items() if k in tracked_numbers
        }
        self._delivered_codes &= tracked_numbers

        if country == "DE" and self._de_session is not None:
            self._prepare_de_poll(tracked)

        # A delivered parcel's payload can never change again, so it is
        # dropped from the fetch — not from ``pairs``/the options list, which
        # stays untouched until the user removes it by hand.
        pairs_to_fetch = [
            (parcel_no, postal_code)
            for parcel_no, postal_code in pairs
            if parcel_no not in self._delivered_codes
        ]
        postal_code_by_number = dict(pairs)

        results = await asyncio.gather(
            *(
                self._client.async_get_parcel(parcel_no, postal_code)
                for parcel_no, postal_code in pairs_to_fetch
            ),
            return_exceptions=True,
        )

        # Each raw payload is kept with its postcode (NL needs it for the
        # tracking deep-link) and its own parcel_no (the group-leaf
        # normalizer needs it as the authoritative barcode source — see
        # tracking/countries/group's docstring) so both can be threaded into
        # normalize_parcel per parcel.
        raws_by_number: dict[str, tuple[dict, str | None, str]] = {}
        errors = 0
        for (parcel_no, postal_code), result in zip(
            pairs_to_fetch, results, strict=True
        ):
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
                    raws_by_number[parcel_no] = (cached, postal_code, parcel_no)
                continue

            if result is None:
                # 204 — unknown or not yet scanned. Keep prior data if we have
                # it, otherwise show a pending placeholder so the user still
                # sees the tracked parcel.
                raws_by_number[parcel_no] = (
                    self._raw_cache.get(parcel_no)
                    or {"parcelNo": parcel_no, "state": None},
                    postal_code,
                    parcel_no,
                )
                continue

            self._raw_cache[parcel_no] = result
            raws_by_number[parcel_no] = (result, postal_code, parcel_no)

        # Parcel numbers skipped from the fetch above (already confirmed
        # delivered) — re-add their cached payload so the delivered sensor
        # keeps its data until the retention filter drops it.
        for parcel_no in self._delivered_codes:
            cached = self._raw_cache.get(parcel_no)
            if cached is not None:
                raws_by_number[parcel_no] = (
                    cached,
                    postal_code_by_number.get(parcel_no),
                    parcel_no,
                )

        if pairs_to_fetch and errors == len(pairs_to_fetch) and not raws_by_number:
            raise UpdateFailed("GLS unreachable for all tracked parcels")

        if country == "DE" and self._de_session is not None:
            self._persist_de_state(tracked)

        include_history = self._include_history
        entries = [
            (
                parcel_no,
                normalize_parcel(
                    raw,
                    postal_code=postal_code,
                    country=country,
                    include_history=include_history,
                    parcel_no=parcel_no,
                ),
            )
            for raw, postal_code, parcel_no in raws_by_number.values()
        ]
        # Rebuilt fresh from this cycle's data — a parcel number whose
        # payload just flipped to delivered is skipped starting next cycle;
        # one that somehow un-delivers (should not happen, but the fetch
        # list must never permanently drop a code) rejoins it automatically.
        self._delivered_codes = {
            parcel_no for parcel_no, p in entries if p["delivered"]
        }
        return self._finish_update(
            entries, successful=not pairs_to_fetch or errors < len(pairs_to_fetch)
        )
