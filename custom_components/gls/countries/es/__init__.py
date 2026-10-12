"""GLS Spain/Portugal / Portugal: signed national consignee transport and mapping.

Spain is not on the pan-EU leaves and cannot be: four real Spanish
consignments were sent to ``rstt029`` with their real postcodes and every one
came back ``404 E800``. Spanish consignments live in a national index behind
``api.consignee.gls-spain.es``, which the same transport serves for Portugal.

Three things make this country structurally different from the rest of the
integration, and each one is why it earns its own package:

* **It signs every request.** The scheme lives in ``signing.py`` so it can be
  proven without a network; see that module for the rule about re-signing.
* **The postcode is half the lookup key**, not hub validation. The service
  matches the number *together with* the postcode, so a wrong postcode is a
  not-found rather than a hint — unlike NL, where it validates, or US/PL,
  where it is never sent at all.
* **One call returns state and full history**, so there is no
  ``rstt029``/``rstt028``-style detail fan-out.

Status is a machine code here, which makes it the better-behaved half of this
family — except for ``incidence``, which is overloaded and has to be read by
its sentence. That is the single highest-value correctness decision on this
surface: mapping ``incidence`` on the code alone reports every ordinary missed
delivery as an exception. Because the sentence decides, the request pins
``Accept-Language`` and never follows the user's HA locale.

**No populated response has been captured.** The field names and the state
vocabulary below are reconstructed, and the transport half (signing, and the
not-found / bad-key / clock-skew branches) is the part that is confirmed on
the wire. Every mapping decision is therefore one-shot-logged so real usage
answers the open questions instead of a second research pass.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    GLS_ES_ACCEPT_LANGUAGE,
    GLS_ES_AGENT,
    GLS_ES_BASE_URL,
    GLS_ES_FIND_PATH,
    GLS_ES_SIGNING_KEY,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)
from .signing import request_timestamp, sign_request

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# ``state.code`` and ``tracking[].code`` draw on one vocabulary, so one map
# serves both levels. Targets are checked against ``ParcelStatus`` itself,
# not against the carrier's own wording -- several of the obvious names
# (accepted, ready_for_pickup, failed_attempt) are not members of this suite's
# enum, and collapsing them onto the nearest real member is deliberate.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "dataReceived": ParcelStatus.REGISTERED,
    "pickupPending": ParcelStatus.REGISTERED,
    "picked": ParcelStatus.IN_TRANSIT,
    "onTransit": ParcelStatus.IN_TRANSIT,
    "partiallyDelivered": ParcelStatus.IN_TRANSIT,
    "rechannelized": ParcelStatus.IN_TRANSIT,
    "onDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "deliveredOnParcel": ParcelStatus.AT_PICKUP_POINT,
    "receivedOnParcel": ParcelStatus.AT_PICKUP_POINT,
    "delivered": ParcelStatus.DELIVERED,
    "onReturn": ParcelStatus.RETURNING,
    "returned": ParcelStatus.RETURNING,
    "retained": ParcelStatus.PROBLEM,
    "broken": ParcelStatus.PROBLEM,
    "destroyed": ParcelStatus.PROBLEM,
    "canceled": ParcelStatus.PROBLEM,
    "pickupIncidence": ParcelStatus.PROBLEM,
}

# ``stored`` is a real code with no milestone of its own: the consumer page
# gives it no rail position, so there is nothing to map it onto. It is left
# out on purpose so it takes the suite's standard fallback -- the newest
# mappable history event -- rather than being forced into a guess.
_UNMAPPED_BY_DESIGN = frozenset({"stored"})

# ``incidence`` covers an ordinary missed delivery as readily as a real
# exception, so the sentence decides and the code only says "read it".
# Exact, whitespace- and case-normalised matches, from the captured English
# wording. Anything unrecognised stays PROBLEM, which is the safe direction:
# it is visible to the user rather than silently optimistic.
_INCIDENCE_MAP: dict[str, ParcelStatus] = {
    "absent": ParcelStatus.PROBLEM,
    "refused by the consignee": ParcelStatus.PROBLEM,
    # Captured on a real parcel (2026-10-12). GLS could not complete the
    # delivery for want of information and the parcel stayed in the network,
    # going out again the next morning. Reported as a problem rather than
    # hidden as in-transit: the carrier raised it, and the group leaf maps its
    # own NOTDELIVERED the same way, so the two surfaces agree.
    "missing data": ParcelStatus.PROBLEM,
    # Captured on a real Portuguese parcel (2026-10-12): the GLS Point itself
    # was shut, so the delivery to it failed and GLS re-routed to the same
    # point an hour later. A failed delivery, like the two above.
    "delivery point non available": ParcelStatus.PROBLEM,
}

# The one incidence sentence that carries a variable tail (the GLS Point's
# name), so it cannot be an exact match. Anchored at the start -- never a
# substring test anywhere in the sentence, which is how the DE text map got
# its "never a substring" rule.
_INCIDENCE_REDIRECT_PREFIX = "shipment redirected to gls point"

_STATE = "state"
_ACCEPT = "application/json"
_CONTENT_TYPE = "application/json; charset=utf-8"

# Everything the reference body carries. A new key is a payload change worth
# reporting, not a reason to fail -- names and types only, never values.
_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "expeditionCode",
        "expeditionUuid",
        "attributes",
        "state",
        "tracking",
        "parcelShop",
        "agency",
        "allowedActions",
        "destination",
        "origin",
        "sender",
        "references",
        "clientReference",
        # Added from the first real Spanish parcel (2026-10-12). None of them
        # feed the canonical shape: ``originAgency`` is the origin depot's
        # code, ``expeditionAliasOption`` is a consumer-page setting,
        # ``whatsapp`` is a notification offer, and ``selectedOption`` records
        # a redirection the recipient chose (and nests its own ``parcelShop``
        # under ``derive`` — not the one the pickup-point rule reads).
        "originAgency",
        "expeditionAliasOption",
        "whatsapp",
        "selectedOption",
    }
)

# The GLS Point is only genuinely a pickup point once one of these scans names
# it. ``parcelShop`` is present on more shipments than that, and ``agency``
# -- the destination depot -- is present on every single one.
_PICKUP_SCAN_CODES = frozenset({"deliveredOnParcel", "receivedOnParcel"})

_unmapped_statuses_logged: set[str] = set()
_unmapped_incidences_logged: set[str] = set()
_unexpected_keys_logged: set[str] = set()
_stored_seen = False
_delivery_date_in_flight_warned = False
_orphan_parcel_shop_warned = False


def _normalise_sentence(value: object) -> str | None:
    """Collapse a status sentence to a comparable form, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split()).casefold()


def _map_incidence(description: object) -> ParcelStatus:
    """Map an ``incidence`` by its sentence, never by its code."""
    sentence = _normalise_sentence(description)
    if sentence is None:
        return ParcelStatus.PROBLEM
    if sentence.startswith(_INCIDENCE_REDIRECT_PREFIX):
        # Diverted to a GLS Point: the parcel is still moving, not in trouble.
        return ParcelStatus.IN_TRANSIT
    mapped = _INCIDENCE_MAP.get(sentence)
    if mapped is not None:
        return mapped
    if sentence not in _unmapped_incidences_logged:
        _unmapped_incidences_logged.add(sentence)
        _LOGGER.warning(
            "Unrecognised GLS Spain/Portugal incidence sentence — help us map it. Open "
            "an issue and paste this line: %s\n  description=%r → reported as "
            "a problem",
            _NEW_ISSUE_URL,
            description,
        )
    return ParcelStatus.PROBLEM


def map_parcel_status_es(code: object, description: object = None) -> ParcelStatus:
    """Map a Spanish state code to the canonical parcel status.

    ``description`` is only read for ``incidence``, the one overloaded code.
    """
    if not isinstance(code, str) or not code.strip():
        return ParcelStatus.UNKNOWN
    value = code.strip()
    if value == "incidence":
        return _map_incidence(description)
    mapped = _STATUS_MAP.get(value)
    if mapped is not None:
        return mapped
    if value in _UNMAPPED_BY_DESIGN:
        global _stored_seen
        if not _stored_seen:
            _stored_seen = True
            _LOGGER.debug(
                "GLS Spain/Portugal reported state %r, which has no milestone of its "
                "own — falling back to the newest history event",
                value,
            )
        return ParcelStatus.UNKNOWN
    if value not in _unmapped_statuses_logged:
        _unmapped_statuses_logged.add(value)
        _LOGGER.warning(
            "Unrecognised GLS Spain/Portugal status — help us map it. Open an issue and "
            "paste this line: %s\n  state.code=%r → falling back to the newest "
            "event",
            _NEW_ISSUE_URL,
            code,
        )
    return ParcelStatus.UNKNOWN


def map_event_status_es(code: object, description: object = None) -> ParcelStatus | None:
    """Map a history row, keeping an unmappable row neutral rather than wrong."""
    status = map_parcel_status_es(code, description)
    return None if status == ParcelStatus.UNKNOWN else status


def _warn_unexpected_top_level_keys(found: dict) -> None:
    """Report unknown keys under ``found`` once, as ``name: type`` pairs only."""
    unknown = sorted(set(found) - _KNOWN_TOP_LEVEL_KEYS)
    pairs = [f"{key}: {type(found[key]).__name__}" for key in unknown]
    fresh = [pair for pair in pairs if pair not in _unexpected_keys_logged]
    if not fresh:
        return
    _unexpected_keys_logged.update(fresh)
    _LOGGER.warning(
        "GLS Spain/Portugal returned unexpected keys under 'found' (names and types "
        "only, safe to paste into an issue at %s): %s",
        _NEW_ISSUE_URL,
        ", ".join(fresh),
    )


async def async_get_parcel_es(
    session: aiohttp.ClientSession, parcel_no: str, postal_code: str
) -> dict[str, Any] | None:
    """Fetch an ES/PT shipment from the signed national consignee route.

    ``postal_code`` is mandatory here, unlike every other country's transport:
    it is half the lookup key, and the service has no recognition surface, so
    a missing or wrong postcode is indistinguishable from a missing parcel.

    Returns ``None`` for the carrier's own semantic not-found, and raises
    ``GlsApiError`` for everything else — a signature rejection, a clock-skew
    rejection and a CDN challenge page are all transport failures, never
    "parcel not found".
    """
    if not postal_code:
        # Not a transport failure and not an absence -- the request cannot be
        # formed. Reported rather than sent, so it is not read as a not-found.
        _LOGGER.warning(
            "GLS Spain/Portugal needs the delivery postcode as well as the number; "
            "parcel skipped this refresh"
        )
        return None

    # Stamped and signed inside the request, never above a retry loop: a
    # reused timestamp is rejected. See ``signing.py``.
    timestamp = request_timestamp()
    signature = sign_request(
        GLS_ES_SIGNING_KEY, "POST", GLS_ES_FIND_PATH, timestamp
    )
    headers = {
        "Content-Type": _CONTENT_TYPE,
        "Accept": _ACCEPT,
        "Accept-Language": GLS_ES_ACCEPT_LANGUAGE,
        "MyGls-Agent": GLS_ES_AGENT,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
    payload = {
        "find": {
            "reference": parcel_no,
            "destination": {"address": {"postalCode": postal_code}},
        }
    }

    async with session.post(
        GLS_ES_BASE_URL + GLS_ES_FIND_PATH, json=payload, headers=headers
    ) as response:
        status = response.status
        try:
            body = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            # The service is reached over a CDN that can interpose a challenge
            # page. HTML is never a missing parcel.
            _LOGGER.warning("GLS Spain/Portugal returned a non-JSON body (HTTP %s)", status)
            raise GlsApiError(status) from None

    error = body.get("error") if isinstance(body, dict) else None
    name = error.get("name") if isinstance(error, dict) else None

    if status == 404 and isinstance(error, dict) and error.get("details") == "notFound":
        # The carrier's own "no shipment for this number and postcode" answer,
        # and the only reply that means the parcel is genuinely absent.
        return None
    if status == 401 and name == "InvalidCredentialsError":
        # The timestamp was outside the service's tolerance. Distinct from a
        # rejected key below, and worth saying so: this one is a clock or a
        # retry that failed to re-sign, not a credential problem.
        _LOGGER.warning(
            "GLS Spain/Portugal rejected the request timestamp — check this machine's "
            "clock. The request is re-signed per attempt, so a persistent "
            "failure here means the clock, not the key"
        )
        raise GlsApiError(status)
    if status == 403 and name == "ForbiddenError":
        # Reported without the value, per the suite rule.
        _LOGGER.warning(
            "GLS Spain/Portugal rejected the request signature. If this persists the "
            "integration needs an update — please report it at %s",
            _NEW_ISSUE_URL,
        )
        raise GlsApiError(status)
    if status != 200:
        raise GlsApiError(status)

    found = body.get("found") if isinstance(body, dict) else None
    if not isinstance(found, dict):
        # A 200 that carries no shipment is not an absence -- the not-found
        # branch above is the only absence -- so it must not drop a sensor.
        _LOGGER.warning(
            "GLS Spain/Portugal returned HTTP 200 with no 'found' object (top-level "
            "type: %s)",
            type(body).__name__,
        )
        raise GlsApiError(status)
    return found


def _parse_instant(value: object) -> datetime | None:
    """Parse a Spanish timestamp, which is a true UTC instant.

    This is the one surface in the GLS family that needs no wall-clock
    reconstruction: ``tracking[].at`` carries ``Z``. Applying the Italian or
    group handling here would shift every Spanish timestamp.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _scans(found: dict) -> list[dict]:
    """Return the history rows, newest first, de-duplicated.

    The reference body repeats a row verbatim with an identical instant, so
    ``(code, description, at)`` is the identity -- not the row's position.
    """
    rows = found.get("tracking")
    if not isinstance(rows, list):
        return []
    seen: set[tuple[object, object, object]] = set()
    unique: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = (row.get("code"), row.get("description"), row.get("at"))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def _history(scans: list[dict]) -> list[dict]:
    """Build the canonical oldest-first history from ``tracking[]``.

    The wire is newest-first and **two rows can carry the identical
    instant** — a real parcel sent "At delivery agency" and "Out for
    delivery" both stamped the same minute, which is the depot scan and the
    van loading moments apart.

    A tie is left in the order the stable sort gives it, which is the
    physically sensible one. That is deliberate rather than lucky, and it is
    now observed rather than assumed: **three** tied pairs across the real
    captures are all the same shape — the carrier lists the agency scan first
    in its newest-first order, so the stable sort emits "At delivery agency"
    then "Out for delivery". Reversing them (what ``countries/group/`` happens
    to do, as a side effect of flipping the list outright rather than as
    anyone's decision) would print those two backwards and read as a defect.
    When two scans share an instant the carrier has stated no order between
    them, so given two arbitrary orders, prefer the one nobody reports as a
    bug.
    """
    parsed: list[tuple[datetime, dict]] = []
    for row in scans:
        moment = _parse_instant(row.get("at"))
        if moment is None:
            continue
        parsed.append(
            (
                moment,
                {
                    "timestamp": moment.isoformat(),
                    "status": map_event_status_es(
                        row.get("code"), row.get("description")
                    ),
                    "raw_status": row.get("description") or row.get("code"),
                },
            )
        )
    parsed.sort(key=lambda item: item[0])
    return [row for _, row in parsed][-HISTORY_MAX_EVENTS:]


def _pickup_point(found: dict, scans: list[dict]) -> str | None:
    """Return the GLS Point's name only when a scan actually names it.

    ``found.agency`` is the destination depot and is present on every
    shipment; ``found.parcelShop`` is present on more than just collections.
    Reading either unconditionally gives every Spanish parcel a phantom
    pickup point -- the same defect that had to be fixed in Correos 1.0.0.
    """
    shop = found.get("parcelShop")
    if not isinstance(shop, dict):
        return None
    if not any(row.get("code") in _PICKUP_SCAN_CODES for row in scans):
        global _orphan_parcel_shop_warned
        if not _orphan_parcel_shop_warned:
            _orphan_parcel_shop_warned = True
            _LOGGER.warning(
                "GLS Spain/Portugal returned a parcelShop with no GLS Point scan to "
                "match it. Please report this at %s — it decides whether the "
                "pickup-point rule is right",
                _NEW_ISSUE_URL,
            )
        return None
    name = shop.get("name")
    return name.strip() or None if isinstance(name, str) else None


def _warn_delivery_date_in_flight_once() -> None:
    """Report a planned date on a parcel that has not been delivered.

    ``state.deliveryDate`` does not say whether it is an estimate or an
    actual, and the only reference body is a delivered parcel. Seeing it on a
    moving parcel is the answer arriving -- so it is reported rather than
    quietly published as an ETA.
    """
    global _delivery_date_in_flight_warned
    if _delivery_date_in_flight_warned:
        return
    _delivery_date_in_flight_warned = True
    _LOGGER.warning(
        "GLS Spain/Portugal reported a deliveryDate on a parcel that is not delivered. "
        "Please report this at %s — it settles whether the field is an "
        "estimate or an actual, and the planned fields stay unset until it does",
        _NEW_ISSUE_URL,
    )


def _sender(found: dict) -> str | None:
    """Return the sending party's name, or ``None``.

    Both real captures carry a business here (a shipper's office), never a
    private person, and NL/CA/DE all populate this field wherever their
    payload has it — so leaving it empty for Spain would be an inconsistency
    rather than a privacy measure. It is redacted from diagnostics like every
    other party field.
    """
    sender = found.get("sender")
    if not isinstance(sender, dict):
        return None
    name = sender.get("name")
    return name.strip() or None if isinstance(name, str) else None


def _weight(found: dict) -> float | None:
    """Return the shipment weight as a number, or ``None``."""
    attributes = found.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get("weight")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def normalize_parcel_es(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "ES",
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Normalize a GLS Spain/Portugal / Portugal response, preserving its full ``raw``."""
    _warn_unexpected_top_level_keys(raw)

    state = raw.get(_STATE)
    state = state if isinstance(state, dict) else {}
    code = state.get("code")
    description = state.get("description")
    status = map_parcel_status_es(code, description)

    scans = _scans(raw)
    newest = scans[0] if scans else None
    if status == ParcelStatus.UNKNOWN and newest is not None:
        # The suite's fallback order: an unmapped code takes the newest
        # mappable history event before it is reported as unknown.
        derived = map_event_status_es(newest.get("code"), newest.get("description"))
        if derived is not None:
            status = derived

    delivered = status == ParcelStatus.DELIVERED
    delivered_at = None
    if delivered:
        moments = [
            moment
            for row in scans
            if row.get("code") == "delivered" and (moment := _parse_instant(row.get("at")))
        ]
        if not moments:
            moments = [
                moment for row in scans if (moment := _parse_instant(row.get("at")))
            ]
        delivered_at = max(moments).isoformat() if moments else None

    if not delivered and state.get("deliveryDate"):
        _warn_delivery_date_in_flight_once()

    # ``barcode`` is the code the user tracked, and for Spain that is a
    # deliberate divergence from the suite's "always take the response's own
    # number" rule -- so don't "align" it with PL or NL.
    #
    # Those two carriers return the *same* identifier the user holds, give or
    # take a check digit. Spain returns a different one: two real parcels were
    # looked up by their 14-digit parcel barcode and answered with a 10-digit
    # ``expeditionCode``, the **shipment** number, which the recipient never
    # sees. Both forms resolve, so there is nothing to gain by switching --
    # and the per-parcel sensor's unique_id is built from this field, so
    # switching would rename every Spanish entity once, to a number its owner
    # does not recognise. ``expeditionCode`` stands in only if the user's own
    # code is somehow missing; it is an int on the wire, hence the coercion.
    barcode = parcel_no
    if not barcode:
        canonical = raw.get("expeditionCode")
        if isinstance(canonical, (int, str)) and not isinstance(canonical, bool):
            barcode = str(canonical).strip() or None

    template = COUNTRIES.get(country, {}).get("tracking_url") or TRACKING_URL
    url = None
    if barcode:
        # ES/PT's own template needs the postcode as well as the number, the
        # way NL's does; without one there is no honest link to give, because
        # the generic GROUP fallback does not hold Spanish consignments.
        if "{postal_code}" in template:
            url = template.format(parcel_no=barcode, postal_code=postal_code or "")
            if not postal_code:
                url = None
        else:
            url = template.format(parcel_no=barcode)

    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": _sender(raw),
        # No recipient name on this surface. ``state.receiverName`` is whoever
        # signed for the parcel, which is not the addressee, and the
        # destination block carries a locality and nothing else.
        "receiver": None,
        "status": status,
        "raw_status": code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        # Left unset on purpose until a moving parcel settles whether
        # ``state.deliveryDate`` is an estimate or an actual.
        "planned_from": None,
        "planned_to": None,
        "pickup": status == ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point(raw, scans),
        "url": url,
        "weight": _weight(raw),
        # No dimensions field on this surface.
        "dimensions": None,
        "history": _history(scans) if include_history else None,
        "raw": raw,
    }


__all__ = [
    "async_get_parcel_es",
    "map_event_status_es",
    "map_parcel_status_es",
    "normalize_parcel_es",
]
