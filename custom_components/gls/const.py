"""Constants for the GLS parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "gls"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    Mirrors the enum the other suite integrations (DHL, DPD, PostNL)
    publish on the ``status`` field of each normalised parcel, so
    cross-carrier automations and the aggregator can target
    ``status: out_for_delivery`` regardless of carrier. Listed in roughly
    the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Arrived at a GLS ParcelShop, ready to collect
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Public GLS tracking endpoint (no auth). Keyed on the parcel number + the
# delivery postal code, so it only covers parcels delivered to an address in
# the selected country. Returns 200 + JSON for a known parcel, or HTTP 204
# (no content) for an unknown / not-yet-scanned one. The ``{host}`` and
# ``{culture}`` come from the hub's country (see ``COUNTRIES``).
PARCEL_DETAILS_URL = (
    "https://{host}/api/tracktrace/v1/"
    "{parcel_no}/postalcode/{postal_code}/details/{culture}"
)

# The delivery country of a hub, chosen at setup time. Only the Netherlands
# exposes a public, postcode-keyed JSON endpoint today; other GLS countries
# either do not expose one or gate it behind Cloudflare / API registration.
# A country is added here once a working endpoint is confirmed — users
# request theirs via the GitHub issue link shown in the setup form.
CONF_COUNTRY = "country"
DEFAULT_COUNTRY = "NL"

# code -> {label, host, culture, postcode_regex}. ``postcode_regex`` matches
# a normalised (space-stripped, upper-cased) postcode for that country.
COUNTRIES: dict[str, dict[str, str]] = {
    "NL": {
        "label": "Netherlands",
        "host": "apm.gls.nl",
        "culture": "nl-NL",
        "postcode_regex": r"^\d{4}[A-Z]{2}$",
    },
}

# Pre-filled "add my country" GitHub issue, linked from the setup form so
# users can report a working endpoint for their country.
NEW_COUNTRY_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?title=Add%20country%3A%20%3Cyour%20country%3E&labels=enhancement"
)

# Consumer tracking deep-link, used to populate the parcel's ``url`` field.
TRACKING_URL = "https://gls-group.com/GROUP/en/parcel-tracking?match={parcel_no}"

# Tracked parcels live in the config entry options as a list of
# ``{parcel_no, postal_code}`` dicts — GLS has no account/feed, the user
# enters the codes themselves.
CONF_PARCELS = "parcels"
CONF_PARCEL_NO = "parcel_no"
CONF_POSTAL_CODE = "postal_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — mirrors the other suite carriers.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls GLS.
# Default 30 min keeps the load on the public endpoint gentle; the minimum
# is 15 min for the same reason. Kept identical to the other suite carriers.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# Per-parcel status history is opt-in and off by default, kept identical to
# the other suite carriers. GLS returns the timeline in the same call, so no
# extra request is involved either way.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
