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

# Which optional contract fields each of this carrier's backends actually
# populates — feeds the comparison table on the docs site, one row per
# country. Keyed by the same label the country selector shows the user;
# order here is display order. Keep each value in lockstep with its
# normalize_parcel_<cc>() in countries/<cc>/__init__.py:
#   Netherlands  — full support: weight, dimensions and the ETA delivery
#                  window (deliveryStatus.etaTimestampMin/Max) all populate.
#   Germany      — weight/dimensions are not in the DTO (confirmed absent by
#                  capture) and there is no delivery-window field either;
#                  pickup_point, url and history still populate.
#   Other        — the pan-EU group-leaf backend (CZ is the first country
#                  routed through it, BUILD_PLAN_CZ.md §3). Populates weight
#                  (infos[] WEIGHT) and url/history, but not dimensions,
#                  delivery_window or pickup_point — the leaf response has no
#                  dimensions/ETA field, and a locker/shop name is only
#                  unstructured text inside history[]'s address, not a
#                  structured pickup_point.
# This used to be a single CAPABILITIES = the intersection across countries,
# which meant NL's full support was invisible on the docs site the moment a
# second, weaker country landed. Per-variant rows fixed that (2026-08-23) —
# see https://github.com/ha-parcel-integrations/ha-parcel-integrations.github.io.
CAPABILITIES_BY_VARIANT = {
    "Netherlands": frozenset(
        {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
    ),
    "Germany": frozenset({"pickup_point", "url", "history"}),
    "Other": frozenset({"weight", "url", "history"}),
}


class GlsApiError(Exception):
    """Raised when a GLS parcel-backend call returns an unexpected status.

    Shared across every country's transport (``countries/nl/``,
    ``countries/de/``) so ``coordinator.py``'s single
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

# The pan-EU GLS group leaves behind the web tracker (group-rest.md) — keyless,
# host-agnostic, and partitioned by the *consignment record*, not by the
# ``{ISO2}/{lang}`` path segment (a locale switch only). CZ is the first
# ``COUNTRIES`` row routed here (its own package is ``countries/cz/``), but
# nothing below names Czech Republic: a later group-leaf country is meant to
# be one more ``COUNTRIES`` row pointing at the same transport, not a copy of
# it (BUILD_PLAN_CZ.md §9/§10). ``rstt028`` (primary) additionally carries
# ``history``/weight/references and needs the delivery ``postalCode``;
# ``rstt029`` (fallback, used only on an ``E609`` postcode mismatch) resolves
# by AWB alone. ``caller=witt002`` is the web tracker's fixed client id, not a
# credential — nothing issues or revokes it per user. ``millis`` is a
# cache-buster (``int(time.time() * 1000)``); no value has ever been
# rejected.
GLS_GROUP_RSTT028_URL = (
    "https://{host}/app/service/open/rest/{group_locale}/rstt028/{awb}"
    "?caller=witt002&millis={millis}&tuOwnerCode=&postalCode={postal_code}"
)
GLS_GROUP_RSTT029_URL = (
    "https://{host}/app/service/open/rest/{group_locale}/rstt029"
    "?match={awb}&type=&caller=witt002&millis={millis}"
)

# The delivery country of a hub, chosen at setup time. Only the Netherlands
# exposes a public, postcode-keyed JSON endpoint today; other GLS countries
# either do not expose one or gate it behind Cloudflare / API registration.
# A country is added here once a working endpoint is confirmed — users
# request theirs via the GitHub issue link shown in the setup form.
CONF_COUNTRY = "country"
DEFAULT_COUNTRY = "NL"

# code -> {host, culture, postcode_regex, postcode_example, tracking_url}.
# ``postcode_regex`` matches a normalised (space-stripped, upper-cased)
# postcode for that country. ``postcode_example`` is shown in the setup form's
# help text and invalid-postcode error, so it must match the country actually
# selected (ha-parcel-integrations/ha-gls#4: it used to be hard-coded to NL's
# format regardless of country). ``tracking_url`` is the consumer deep-link for
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
#
# CZ has no ``culture`` key at all — deliberately. For NL/DE, ``culture`` is a
# ``nl-NL``-style locale in a *national* URL template; on the group leaves
# (group-rest.md) the ``{ISO2}/{lang}`` path segment is a locale switch over
# the *same* pan-EU index, not a data partition, and status mapping keys off
# the locale-independent ``progressBar.statusInfo``. Overloading ``culture``
# for that would blur two different concepts, so group-leaf countries get
# their own ``group_locale`` key instead (BUILD_PLAN_CZ.md §6's decision) —
# consumed by ``countries/cz/``'s transport, which any future group-leaf
# country (SI/RO/HU/SK) is meant to share by adding one more row here.
COUNTRIES: dict[str, dict[str, str]] = {
    "NL": {
        "host": "apm.gls.nl",
        "culture": "nl-NL",
        "postcode_regex": r"^\d{4}[A-Z]{2}$",
        "postcode_example": "1234AB",
        "tracking_url": (
            "https://www.gls-info.nl/tracking"
            "?trackid={parcel_no}&zipcode={postal_code}"
        ),
    },
    "DE": {
        "host": GLS_DE_TRACKINGS_HOST,
        "culture": "de-DE",
        "postcode_regex": r"^\d{5}$",
        "postcode_example": "12345",
    },
    "CZ": {
        "host": "gls-group.com",  # .eu and .com are interchangeable (group-rest.md)
        "group_locale": "CZ/en",
        "postcode_regex": r"^\d{3}\s?\d{2}$",
        "postcode_example": "254 01",
        "tracking_url": (
            "https://gls-group.eu/CZ/en/parcel-tracking?match={parcel_no}"
        ),
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
# Standard service field name shared by every parcel-suite carrier.
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
