"""GLS parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GlsApiClient
from .const import PLATFORMS
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
    # No auth: GLS-NL tracking is public, so the HA-managed session is fine.
    client = GlsApiClient(async_get_clientsession(hass))
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
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Single-entry integration: no other entry can still need the
        # services, so remove them alongside the entry.
        async_unload_services(hass)
        return True
    return False
