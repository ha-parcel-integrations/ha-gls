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
    "trackingReference",
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
    # deliveryPreference nests the recipient's email (under contactValues[].
    # value), address and preference UUIDs — redact the whole block.
    "deliveryPreference",
    "consignee",
    "contactValues",
    "houseNumber",
    "houseNumberAddition",
    # DE's persisted group parcelNumber (tuNo) — same identifier class as
    # parcelNo/uniqueNo/gpNo above.
    "de_parcel_number",
    "parcelNumber",
    # DE's stable cross-app-instance parcel uuid (germany.md) — same
    # identifier class as parcelNumber above.
    "id",
    # The *canonical* (normalize_parcel_*) top-level fields, not just the
    # raw payload's own key spellings above. Without these, a parcel dict's
    # "raw" sub-tree gets redacted but its own "barcode"/"sender"/"receiver"/
    # "pickup_point"/"url" siblings don't — and "url" embeds the tracking
    # number and postal code as literal query-string values (NL's
    # gls-info.nl deep-link, DE/generic gls-group.com's), which
    # async_redact_data can't partially scrub, so the whole field is
    # redacted rather than left exposed.
    "barcode",
    "sender",
    "receiver",
    "pickup_point",
    "url",
    # GLS Germany's guest-account identity (BUILD_PLAN_DE.md §3). Not a
    # secret in the credential sense (anyone can mint one), but it's a
    # stable per-install identifier — redact it anyway. It lives in
    # entry.data (de_app_instance_id) and, defensively, under any of these
    # key spellings in case a token or the id ever rides along in a raw
    # payload. The session's live bearer token is never persisted to
    # entry.data/entry.options at all, so it can't appear in "entry_data"
    # below; these keys are a defensive net for "raw" all the same.
    "de_app_instance_id",
    "appInstanceId",
    "accessToken",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GlsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a GLS config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
