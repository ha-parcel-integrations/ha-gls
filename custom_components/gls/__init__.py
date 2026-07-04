"""GLS parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GlsApiClient
from .const import (
    CONF_COUNTRY,
    CONF_POSTAL_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GlsCoordinator, _refresh_interval
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class GlsData:
    """Runtime data attached to a GLS config entry."""

    client: GlsApiClient
    coordinator: GlsCoordinator


type GlsConfigEntry = ConfigEntry[GlsData]


async def async_setup_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Set up GLS from a config entry."""
    # Entries from before the multi-hub redesign carry ``unique_id = DOMAIN``;
    # the config flow now dedupes hubs on their postcode, so migrate the
    # legacy id or a second hub with the same postcode could be added.
    if entry.unique_id == DOMAIN and (
        postal_code := entry.options.get(CONF_POSTAL_CODE)
    ):
        hass.config_entries.async_update_entry(entry, unique_id=postal_code)

    # No auth: GLS tracking is public, so the HA-managed session is fine. The
    # endpoint host + culture come from the hub country; entries created
    # before the country option default to the Netherlands.
    country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    country_cfg = COUNTRIES.get(country, COUNTRIES[DEFAULT_COUNTRY])
    client = GlsApiClient(
        async_get_clientsession(hass),
        host=country_cfg["host"],
        culture=country_cfg["culture"],
    )
    coordinator = GlsCoordinator(hass, client, entry)

    entry.runtime_data = GlsData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, interval, history) live via
    # a coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: GlsConfigEntry) -> None:
    """Apply changed options: retune the interval and refresh the coordinator."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _refresh_interval(entry)
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Unload a GLS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared across hubs, so only remove them once the last
    # hub is gone — otherwise unloading one hub would break the others.
    others_loaded = any(
        other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not others_loaded:
        async_unload_services(hass)
    return True
