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

from .config_flow import (
    normalize_parcel_no,
    normalize_postcode,
    valid_parcel_no,
    valid_postcode,
)
from .const import (
    CONF_COUNTRY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DEFAULT_COUNTRY,
    DOMAIN,
)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PARCEL_NO): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_PARCEL_NO): cv.string})


def _resolve_entry(hass: HomeAssistant, postal_code: str | None):
    """Pick the GLS hub to act on.

    With one hub, that hub. With several, the ``postal_code`` argument selects
    it; if omitted and ambiguous, raise so the caller knows to specify one.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
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


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the GLS services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        parcel_no = normalize_parcel_no(call.data[CONF_PARCEL_NO])
        if not valid_parcel_no(parcel_no):
            raise ServiceValidationError(f"'{parcel_no}' is not a valid parcel number")
        entry = _resolve_entry(hass, call.data.get(CONF_POSTAL_CODE))
        postal_code = normalize_postcode(
            call.data.get(CONF_POSTAL_CODE)
            or entry.options.get(CONF_POSTAL_CODE, "")
        )
        country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        if not valid_postcode(postal_code, country):
            raise ServiceValidationError(f"'{postal_code}' is not a valid postal code")

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_PARCEL_NO] == parcel_no for p in parcels):
            return  # already tracked — no-op
        parcels.append({CONF_PARCEL_NO: parcel_no, CONF_POSTAL_CODE: postal_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        parcel_no = normalize_parcel_no(call.data[CONF_PARCEL_NO])
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError("GLS is not set up")
        # Remove the parcel from whichever hub(s) track it.
        for entry in entries:
            current = entry.options.get(CONF_PARCELS, [])
            kept = [p for p in current if p[CONF_PARCEL_NO] != parcel_no]
            if len(kept) != len(current):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
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
