"""Button platform for the GLS parcel tracker integration."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GlsConfigEntry
from .const import CONF_POSTAL_CODE, DOMAIN
from .device import build_device_info

# A manual refresh is a single API round-trip per tracked parcel; HA's
# per-entity throttling adds nothing here.
PARALLEL_UPDATES = 0




async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the GLS refresh button from a config entry."""
    async_add_entities([GlsRefreshButton(entry)])


class GlsRefreshButton(ButtonEntity):
    """Button that forces an immediate poll of all tracked GLS parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_attribution = "Data provided by GLS"

    def __init__(self, entry: GlsConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = build_device_info(entry)

    async def async_press(self) -> None:
        """Trigger an immediate refresh of the coordinator."""
        await self._entry.runtime_data.coordinator.async_request_refresh()
