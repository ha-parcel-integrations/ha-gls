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

# Public GLS Netherlands tracking endpoint (no auth). Keyed on the parcel
# number + the delivery postal code, so this only covers parcels delivered
# to a Dutch address. Returns 200 + JSON for a known parcel, or HTTP 204
# (no content) for an unknown / not-yet-scanned one.
PARCEL_DETAILS_URL = (
    "https://apm.gls.nl/api/tracktrace/v1/"
    "{parcel_no}/postalcode/{postal_code}/details/{culture}"
)
CULTURE = "nl-NL"

# Consumer tracking deep-link, used to populate the parcel's ``url`` field.
TRACKING_URL = "https://gls-group.com/GROUP/en/parcel-tracking?match={parcel_no}"

# Tracked parcels live in the config entry options as a list of
# ``{parcel_no, postal_code}`` dicts — GLS has no account/feed, the user
# enters the codes themselves.
CONF_PARCELS = "parcels"
CONF_PARCEL_NO = "parcel_no"
CONF_POSTAL_CODE = "postal_code"

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
