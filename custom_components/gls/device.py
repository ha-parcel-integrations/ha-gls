"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry. It used to be defined three times — once per platform —
with the button and calendar docstrings noting that they mirrored the sensor's
copy, which is exactly the kind of duplication that drifts.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_POSTAL_CODE, DOMAIN


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity for this GLS hub.

    The postal code is part of the device name so multiple hubs (e.g. home
    and work) stay distinguishable — mirroring the account-in-name pattern
    of the other carriers.
    """
    postal_code = entry.options.get(CONF_POSTAL_CODE, "")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"GLS ({postal_code})" if postal_code else "GLS",
        manufacturer="GLS",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://gls-group.com",
    )
