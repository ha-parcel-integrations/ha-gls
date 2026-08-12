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

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py (which dispatches per country): this is the
# INTERSECTION across countries, not just the NL default — a field only
# belongs here if every dispatched country populates it, so the table never
# overclaims for a GLS DE user. NL populates weight, dimensions and the
# delivery window; DE's normalize_parcel_de does not (still `None` there), so
# those three stay out. Both countries populate pickup_point, url and history.
CAPABILITIES = frozenset({"pickup_point", "url", "history"})


class GlsApiError(Exception):
    """Raised when a GLS parcel-backend call returns an unexpected status.

    Shared across every country's transport (``countries/nl.py``,
    ``countries/de.py``) so ``coordinator.py``'s single
    ``except (GlsApiError, aiohttp.ClientError)`` branch keeps working
    regardless of which country produced the failure. Lives here rather than
    in ``api.py`` so a country module can raise it without importing
    ``api.py`` (which itself imports the country modules — see
    ``countries/__init__.py``).
    """

    def __init__(self, status_code: int) -> None:
        """Store the status code that triggered the error."""
        super().__init__(f"GLS API request failed with status {status_code}")
        self.status_code = status_code


# Public GLS tracking endpoint (no auth). Keyed on the parcel number + the
# delivery postal code, so it only covers parcels delivered to an address in
# the selected country. Returns 200 + JSON for a known parcel, or HTTP 204
# (no content) for an unknown / not-yet-scanned one. The ``{host}`` and
# ``{culture}`` come from the hub's country (see ``COUNTRIES``).
PARCEL_DETAILS_URL = (
    "https://{host}/api/tracktrace/v1/"
    "{parcel_no}/postalcode/{postal_code}/details/{culture}"
)

# GLS Germany has no keyless endpoint: every route on the national parcel
# backend needs the anonymous bearer token ``countries/de/session.py`` mints
# (``guest-account``, not ``auth: none`` — see
# carrier-research/api/gls/germany.md). Two hosts: the parcel backend, and a
# group-wide identity service with no country in its path.
GLS_DE_TRACKINGS_HOST = "gls-pakete-de-backend-app.ooh.glsnxt.com"
# Adds a parcel to the app instance *and* returns its full details in the
# same response (200) — the route a code-based integration drives. A repeat
# call for an already-tracked parcel answers 409 with no body.
GLS_DE_TRACKINGS_ADD_URL = f"https://{GLS_DE_TRACKINGS_HOST}/api/v1/trackings"
# Keyed on ``parcelNumber`` (the group ``tuNo``) or the ``id`` UUID — NOT the
# ``trackingReference`` the user typed, which 404s here.
GLS_DE_TRACKING_DETAIL_URL = (
    f"https://{GLS_DE_TRACKINGS_HOST}/api/v1/trackings/{{parcel_number}}"
)
GLS_DE_IDENTITY_HOST = "api-backend.glsnxt.com"
# The one hardcoded exception in the app's auth interceptor: neither of these
# two routes ever carries an ``Authorization`` header.
GLS_DE_REGISTER_URL = (
    f"https://{GLS_DE_IDENTITY_HOST}/ecosystem/user-service/v1/users/register"
)
GLS_DE_VALIDATE_URL = (
    f"https://{GLS_DE_IDENTITY_HOST}/ecosystem/user-service/v1/users/validate"
)

# The delivery country of a hub, chosen at setup time. Only the Netherlands
# exposes a public, postcode-keyed JSON endpoint today; other GLS countries
# either do not expose one or gate it behind Cloudflare / API registration.
# A country is added here once a working endpoint is confirmed — users
# request theirs via the GitHub issue link shown in the setup form.
CONF_COUNTRY = "country"
DEFAULT_COUNTRY = "NL"

# code -> {label, host, culture, postcode_regex, tracking_url}.
# ``postcode_regex`` matches a normalised (space-stripped, upper-cased)
# postcode for that country. ``tracking_url`` is the consumer deep-link for
# the parcel's ``url`` field: the generic ``gls-group.com`` link intermittently
# returns "package not found" for NL parcels, so NL points at the country site
# ``gls-info.nl``, which needs the postcode as well as the parcel number.
# Countries without a specific entry fall back to ``TRACKING_URL`` below.
#
# DE's ``host``/``culture`` are carried here for schema consistency with NL,
# but its transport (``countries/de.py``) does not build a URL from them the
# way NL's ``{host}``/``{culture}`` PARCEL_DETAILS_URL does — DE is a bearer
# POST against GLS_DE_TRACKINGS_ADD_URL, and ``culture`` only pins the
# ``Accept-Language`` header (germany.md: the event text is localized by it,
# and logic must never key off that text). DE has no ``tracking_url`` entry:
# BUILD_PLAN_DE.md §4 says its ``url`` reuses the generic ``TRACKING_URL``
# fallback below directly, keyed on ``parcelNumber``.
COUNTRIES: dict[str, dict[str, str]] = {
    "NL": {
        "host": "apm.gls.nl",
        "culture": "nl-NL",
        "postcode_regex": r"^\d{4}[A-Z]{2}$",
        "tracking_url": (
            "https://www.gls-info.nl/tracking"
            "?trackid={parcel_no}&zipcode={postal_code}"
        ),
    },
    "DE": {
        "host": GLS_DE_TRACKINGS_HOST,
        "culture": "de-DE",
        "postcode_regex": r"^\d{5}$",
    },
}

# Linked from the setup form so users can ask for a country we don't cover
# yet. Country/carrier requests go through the organisation discussion (the
# suite's standard "how a carrier arrives" channel), never a direct issue —
# same URL as this repo's own .github/ISSUE_TEMPLATE/config.yml contact link.
REQUEST_COUNTRY_URL = "https://github.com/ha-parcel-integrations/.github/discussions/new/choose"

# Generic fallback tracking deep-link, used for countries without a specific
# ``tracking_url`` in ``COUNTRIES`` (or when the postcode is unknown). Note this
# link is unreliable for NL parcels — see the per-country ``tracking_url``.
TRACKING_URL = "https://gls-group.com/GROUP/en/parcel-tracking?match={parcel_no}"

# Tracked parcels live in the config entry options as a list of
# ``{parcel_no, postal_code}`` dicts — GLS has no account/feed, the user
# enters the codes themselves. DE entries additionally carry an optional
# ``de_parcel_number`` once learned (see below) — NL entries never have it.
CONF_PARCELS = "parcels"
CONF_PARCEL_NO = "parcel_no"
CONF_POSTAL_CODE = "postal_code"

# DE only. The group ``parcelNumber`` (``tuNo``) a tracked parcel resolved to
# on its first successful ``POST`` add — persisted here (not just in
# ``countries/de/__init__.py``'s in-process cache) so a later HA restart can
# poll it by ``GET`` straight away instead of re-hitting the "already
# tracked, no way to recover its id" 409 gap once more. Set by
# ``coordinator.py``, read by ``coordinator.py`` to seed the transport's own
# cache before polling.
CONF_DE_PARCEL_NUMBER = "de_parcel_number"

# DE only. The self-minted anonymous ``appInstanceId`` (BUILD_PLAN_DE.md §3)
# lives in ``entry.data``, not ``entry.options`` — it is not a user
# preference, options are rewritten on every parcel add/remove, and
# ``entry.data`` is what ``async_migrate_entry`` already knows how to move.
CONF_DE_APP_INSTANCE_ID = "de_app_instance_id"
# Standard service field name shared by every parcel-suite carrier. GLS's
# services accept ``tracking_code``; the old ``parcel_no`` field is a
# deprecated alias (see services.py) kept working for now and removed soon.
CONF_TRACKING_CODE = "tracking_code"

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
