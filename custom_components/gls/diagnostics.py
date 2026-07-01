"""Diagnostics support for the GLS parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GlsConfigEntry

TO_REDACT = {
    "parcel_no",
    "postal_code",
    "parcelNo",
    "shipmentNo",
    "shipmentUniqueNo",
    "gpNo",
    "uniqueNo",
    "reference",
    "zipcode",
    "zipCode",
    "postalCode",
    "city",
    "street",
    "houseNo",
    "houseNoAdd",
    "name",
    "name2",
    "name3",
    "email",
    "gpsLocation",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GlsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a GLS config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
