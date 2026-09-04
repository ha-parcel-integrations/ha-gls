"""Diagnostics support for the GLS parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
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
    # DE's stable cross-app-instance parcel uuid — same
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
    # GLS Germany's guest-account identity. Not a
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
    # Group-leaf identifiers that carry the same parcel number as
    # parcelNo/uniqueNo above (ha-gls#6): referenceNo/tuNo are flat
    # top-level keys, redacted here directly; references[]'s UNITNO entry
    # is typed by a sibling "type" field and handled in
    # _redact_group_sensitive below alongside CUSTREF.
    "referenceNo",
    "tuNo",
}


def _redact_group_sensitive(data: Any) -> Any:
    """Redact the group-leaf ``references[].value`` (CUSTREF/UNITNO) and ``signature.value``.

    Both live too deep/context-dependently for the flat ``TO_REDACT`` key set
    above: a ``references[]`` entry is typed by a sibling ``type`` field, so
    redacting every ``value`` key outright would also blank the harmless
    WEIGHT one; ``signature.value`` needs a nested, key-specific match. Runs
    as its own recursive pass before ``async_redact_data`` so the generic
    flat-key pass still handles ``postalCode``/``barcode``/``url``/… untouched.
    A no-op for NL/DE data — neither ever carries a ``references[]`` entry
    typed ``CUSTREF``/``UNITNO`` or a ``signature`` dict. ``UNITNO`` carries
    the same parcel number as ``referenceNo``/``tuNo`` (ha-gls#6).
    """
    if isinstance(data, list):
        return [_redact_group_sensitive(item) for item in data]
    if isinstance(data, dict):
        redacted = dict(data)
        if redacted.get("type") in ("CUSTREF", "UNITNO") and "value" in redacted:
            redacted["value"] = REDACTED
        signature = redacted.get("signature")
        if isinstance(signature, dict) and "value" in signature:
            redacted["signature"] = {**signature, "value": REDACTED}
        return {key: _redact_group_sensitive(value) for key, value in redacted.items()}
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GlsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a GLS config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "polling": {
            "current_tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(
            _redact_group_sensitive(coordinator.data or []), TO_REDACT
        ),
        "delivered": async_redact_data(
            _redact_group_sensitive(coordinator.delivered or []), TO_REDACT
        ),
    }
