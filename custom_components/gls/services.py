"""Services for the GLS parcel tracker integration.

`gls.track_parcel` / `gls.untrack_parcel` let you add or remove a tracked
parcel without opening the integration options — so a Lovelace button can
start tracking a parcel straight from a dashboard. `gls.update_contents` sets
or clears the text describing what is in a parcel, tracked or not, on the
hub(s) or account(s) that list the code.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import (
    normalize_parcel_no,
    normalize_postcode,
    valid_parcel_no,
    valid_postcode,
)
from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_COUNTRY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    DEFAULT_COUNTRY,
    DOMAIN,
    is_account_entry,
)
from .contents import ContentsStore, normalize_code

_LOGGER = logging.getLogger(__name__)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
    }
)


def _resolve_entry(hass: HomeAssistant, postal_code: str | None):
    """Pick the GLS hub to act on.

    With one hub, that hub. With several, the ``postal_code`` argument selects
    it; if omitted and ambiguous, raise so the caller knows to specify one.
    """
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if not is_account_entry(entry)
    ]
    if not entries:
        raise ServiceValidationError("GLS is not set up")
    if postal_code:
        target = normalize_postcode(postal_code)
        for entry in entries:
            if entry.options.get(CONF_POSTAL_CODE) == target:
                return entry
        raise ServiceValidationError(f"No GLS hub for postal code {target}")
    if len(entries) == 1:
        return entries[0]
    raise ServiceValidationError(
        "Multiple GLS hubs are set up — pass postal_code to choose one"
    )


def _loaded_coordinator(entry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("GLS is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def async_setup_services(hass: HomeAssistant, *, hub: bool = True) -> None:
    """Register the GLS services (idempotent).

    ``update_contents`` serves every entry; ``track_parcel`` and
    ``untrack_parcel`` belong to tracking hubs, so an account entry passes
    ``hub=False``.
    """

    async def _track(call: ServiceCall) -> None:
        parcel_no = normalize_parcel_no(call.data[CONF_TRACKING_CODE])
        if not valid_parcel_no(parcel_no):
            raise ServiceValidationError(f"'{parcel_no}' is not a valid parcel number")
        entry = _resolve_entry(hass, call.data.get(CONF_POSTAL_CODE))
        postal_code = normalize_postcode(
            call.data.get(CONF_POSTAL_CODE) or entry.options.get(CONF_POSTAL_CODE, "")
        )
        country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        if not valid_postcode(postal_code, country):
            raise ServiceValidationError(f"'{postal_code}' is not a valid postal code")

        has_contents = CONF_CONTENTS in call.data
        coordinator = _loaded_coordinator(entry) if has_contents else None

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_PARCEL_NO] == parcel_no for p in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                parcel_no, call.data[CONF_CONTENTS]
            ):
                coordinator.async_reapply_contents()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(parcel_no, call.data[CONF_CONTENTS])
        parcels.append({CONF_PARCEL_NO: parcel_no})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        parcel_no = normalize_parcel_no(call.data[CONF_TRACKING_CODE])
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if not is_account_entry(entry)
        ]
        if not entries:
            raise ServiceValidationError("GLS is not set up")
        # Remove the parcel from whichever hub(s) track it.
        for entry in entries:
            if (store := _contents_store(entry)) is None:
                # The file outlives an unloaded entry, so clear it directly.
                store = ContentsStore(hass, entry.entry_id)
                await store.async_load()
            store.async_set(parcel_no, None)
            current = entry.options.get(CONF_PARCELS, [])
            kept = [p for p in current if p[CONF_PARCEL_NO] != parcel_no]
            if len(kept) != len(current):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
                )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("GLS is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(f"'{entry_id}' is not a loaded GLS entry")
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                coordinator.async_reapply_contents()

    if hub and not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            _update_contents,
            schema=_UPDATE_CONTENTS_SCHEMA,
        )


def async_unload_services(
    hass: HomeAssistant, *, hubs_left: bool, entries_left: bool
) -> None:
    """Remove the GLS services their entries no longer need.

    The hub services go with the last tracking hub; ``update_contents`` stays
    while any entry, hub or account, is still loaded.
    """
    services = []
    if not hubs_left:
        services += [SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL]
    if not entries_left:
        services.append(SERVICE_UPDATE_CONTENTS)
    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
