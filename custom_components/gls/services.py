"""Services for the GLS parcel tracker integration.

`gls.track_parcel` / `gls.untrack_parcel` let you add or remove a tracked
parcel without opening the integration options — so a Lovelace button can
start tracking a parcel straight from a dashboard.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .config_flow import normalize_postcode, valid_parcel_no, valid_postcode
from .const import CONF_PARCEL_NO, CONF_PARCELS, CONF_POSTAL_CODE, DOMAIN

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PARCEL_NO): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_PARCEL_NO): cv.string})


def _get_entry(hass: HomeAssistant):
    """Return the single loaded GLS config entry, or raise."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("GLS is not set up")
    return entries[0]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the GLS services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        entry = _get_entry(hass)
        parcel_no = call.data[CONF_PARCEL_NO].strip()
        postal_code = normalize_postcode(
            call.data.get(CONF_POSTAL_CODE)
            or entry.options.get(CONF_POSTAL_CODE, "")
        )
        if not valid_parcel_no(parcel_no):
            raise ServiceValidationError(f"'{parcel_no}' is not a valid parcel number")
        if not valid_postcode(postal_code):
            raise ServiceValidationError(f"'{postal_code}' is not a valid postal code")

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_PARCEL_NO] == parcel_no for p in parcels):
            return  # already tracked — no-op
        parcels.append({CONF_PARCEL_NO: parcel_no, CONF_POSTAL_CODE: postal_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        entry = _get_entry(hass)
        parcel_no = call.data[CONF_PARCEL_NO].strip()
        parcels = [
            p
            for p in entry.options.get(CONF_PARCELS, [])
            if p[CONF_PARCEL_NO] != parcel_no
        ]
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the GLS services (single-entry integration, so on unload)."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
