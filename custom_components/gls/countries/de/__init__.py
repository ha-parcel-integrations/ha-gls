"""GLS Germany: bearer ``POST`` transport, payload mapping and status map.

Built **past** BUILD_PLAN_DE.md's §0 gate — a deliberate, maintainer-approved
decision. The gate asked for one *in-transit* parcel to resolve; only two
*delivered* AWBs have ever been captured, and both came back with
``latestStatusText: ""`` and no status field at all (germany.md). So the
status map below is **derivation-first**, not enum-first (§5): the enum table
has never been seen on the wire and is wired in only as a fallback layer, not
the primary mechanism. Every WARNING obligation in BUILD_PLAN_DE.md §6 is
implemented in full — that logging is how this branch gets corrected once a
real in-transit parcel is finally captured, without a second build session.

Germany needs its own nested package (rather than a flat ``countries/de.py``)
because it also needs a session/token lifecycle module with no NL
equivalent — see :mod:`.session`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ...const import (
    GLS_DE_TRACKING_DETAIL_URL,
    GLS_DE_TRACKINGS_ADD_URL,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)
from .session import GlsDeSession

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# Pin one locale for the (never-used-for-logic) event text — germany.md:
# `Accept-Language` changes `deliveryEvents[].description`, and mixing
# locales would make the one-shot `latestStatusText` pairing log noisier
# than it needs to be.
_ACCEPT_LANGUAGE = "de-DE"


# ---------------------------------------------------------------------------
# Transport — bearer POST/GET, no keyless route (BUILD_PLAN_DE.md §2, §3)
# ---------------------------------------------------------------------------

# trackingReference -> parcelNumber, learned from the first successful add
# (POST -> 200). A repeat POST for an already-tracked parcel answers 409
# with no body, so later polls use GET-by-parcelNumber instead (germany.md:
# "poll per parcel"). In-process only — resets on restart, same lifetime as
# the coordinator's own `_raw_cache`. A cold restart hitting 409 for a
# parcel this instance already tracks from a *previous* session has no way
# to recover `parcelNumber` from the 409 body alone; that surfaces as a
# GlsApiError(409) below and is absorbed by the coordinator's existing
# cached/pending-placeholder handling, same as any other transient failure.
# Durably persisting `parcelNumber` (e.g. alongside `entry.options
# [CONF_PARCELS]`) is coordinator.py/config_flow.py work, deliberately not
# part of this change — see BUILD_PLAN_DE.md §1's file table.
_known_parcel_numbers: dict[str, str] = {}


def _mentions_recaptcha(text: str, headers: Any) -> bool:
    """Whether a response body or headers mention reCAPTCHA."""
    if "recaptcha" in text.lower():
        return True
    return any(str(key).lower() == "x-recaptcha-token" for key in headers)


_recaptcha_warned = False


def _warn_recaptcha_once() -> None:
    """One-shot, loud warning: any mention of reCAPTCHA means the surface may close."""
    global _recaptcha_warned
    if _recaptcha_warned:
        return
    _recaptcha_warned = True
    _LOGGER.warning(
        "GLS Germany's parcel backend response mentions reCAPTCHA — the "
        "guest-account surface may be closing. Open an issue: %s",
        _NEW_ISSUE_URL,
    )


async def _async_do_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    json_body: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int]:
    """One HTTP call against the DE parcel backend; returns (body, status)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": _ACCEPT_LANGUAGE,
    }
    async with session.request(
        method, url, headers=headers, json=json_body
    ) as response:
        text = await response.text()
        if _mentions_recaptcha(text, response.headers):
            _warn_recaptcha_once()
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None
        return body, response.status


async def _async_request(
    session: aiohttp.ClientSession,
    de_session: GlsDeSession,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Request with the session's bearer, refreshing once on a 401.

    Never loops: one retry, then whatever the retry returned (success or
    not) is final — BUILD_PLAN_DE.md §3.3.
    """
    token = await de_session.async_get_token()
    body, status = await _async_do_request(session, method, url, token, json_body)
    if status == 401:
        token = await de_session.async_handle_unauthorized()
        body, status = await _async_do_request(session, method, url, token, json_body)
    return body, status


async def _async_add_parcel(
    session: aiohttp.ClientSession,
    de_session: GlsDeSession,
    tracking_reference: str,
    postal_code: str,
) -> dict[str, Any] | None:
    """``POST /api/v1/trackings`` — add-and-fetch for a not-yet-tracked parcel."""
    body, status = await _async_request(
        session,
        de_session,
        "POST",
        GLS_DE_TRACKINGS_ADD_URL,
        json_body={"trackingReference": tracking_reference, "postcode": postal_code},
    )
    if status == 200:
        return body
    if status == 404:
        # Semantic not-found (wrong number, or the check-digit variant the
        # backend can't resolve) — mirrors NL's 204 -> None.
        return None
    if status == 409:
        # Already tracked, but this call site never learned its
        # parcelNumber (see the module cache note above) — nothing in a 409
        # body identifies it. Surface as a fetch error.
        raise GlsApiError(status)
    raise GlsApiError(status)


async def _async_get_by_parcel_number(
    session: aiohttp.ClientSession,
    de_session: GlsDeSession,
    parcel_number: str,
) -> dict[str, Any] | None:
    """``GET /api/v1/trackings/{parcelNumber}`` — the steady-state poll."""
    url = GLS_DE_TRACKING_DETAIL_URL.format(parcel_number=parcel_number)
    body, status = await _async_request(session, de_session, "GET", url)
    if status == 200:
        return body
    if status == 404:
        return None
    raise GlsApiError(status)


async def async_get_parcel_de(
    session: aiohttp.ClientSession,
    de_session: GlsDeSession,
    tracking_reference: str,
    postal_code: str,
) -> dict[str, Any] | None:
    """Fetch one DE parcel's tracking details.

    Adds the parcel on first use (``POST``, which returns the full body in
    the same response), then polls it by ``parcelNumber`` on every later
    call (``GET``) — a repeat ``POST`` only returns ``409`` with no body,
    and the inbox is not a reliable poll source (§8 "do not build").
    """
    known_parcel_number = _known_parcel_numbers.get(tracking_reference)
    if known_parcel_number is not None:
        body = await _async_get_by_parcel_number(
            session, de_session, known_parcel_number
        )
    else:
        body = await _async_add_parcel(
            session, de_session, tracking_reference, postal_code
        )

    if body is not None:
        parcel_number = body.get("parcelNumber")
        if parcel_number:
            _known_parcel_numbers[tracking_reference] = parcel_number
    return body


# ---------------------------------------------------------------------------
# Status mapping — derivation-first (maintainer decision, build past §0)
# ---------------------------------------------------------------------------

# Never seen on the wire (§5) — a fallback layer only, consulted when the
# derivation below doesn't already settle the status. Do NOT borrow the
# `rstt029` vocabulary (group-rest.md): different mechanism, different
# strings (`OUT_FOR_DELIVERY` vs `INDELIVERY`).
_ENUM_MAP: dict[str, ParcelStatus] = {
    "OUT_FOR_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVERED": ParcelStatus.DELIVERED,
    "NOT_DELIVERED": ParcelStatus.PROBLEM,
    "BLOCKED": ParcelStatus.PROBLEM,
    "UNKNOWN": ParcelStatus.UNKNOWN,
}

# A `DELIVERED_TO_*` member mentioning one of these is the ParcelShop variant
# (still awaiting collection); the exact member names were never fully
# recovered (§5), so this matches by substring rather than an exact set.
_SHOP_HINTS = ("SHOP", "PARCELSHOP", "PICKUP")
_HANDOVER_HINTS = ("NEIGHBOUR", "NEIGHBOR", "DEPOSIT")

_unmapped_enum_logged: set[str] = set()
_unmapped_delivered_to_logged: set[str] = set()
_status_text_pairs_logged: set[str] = set()
_unexpected_keys_logged: set[str] = set()
_time_frame_type_logged = False


def _warn_unmapped_enum(value: str) -> None:
    """One-shot warning for a DE status enum value outside §5 (§6)."""
    if value in _unmapped_enum_logged:
        return
    _unmapped_enum_logged.add(value)
    _LOGGER.warning(
        "Unrecognised GLS Germany status value — help us map it. Open an "
        "issue and paste this line: %s\n  value=%s → reported as 'unknown'",
        _NEW_ISSUE_URL,
        value,
    )


def _warn_unmapped_delivered_to(value: str) -> None:
    """One-shot warning for a DELIVERED_TO_* member the shop/neighbour branch misses (§6)."""
    if value in _unmapped_delivered_to_logged:
        return
    _unmapped_delivered_to_logged.add(value)
    _LOGGER.warning(
        "Unrecognised GLS Germany DELIVERED_TO_* member — help us classify "
        "it as a shop handover or a direct one. Open an issue and paste "
        "this line: %s\n  value=%s → reported as 'delivered'",
        _NEW_ISSUE_URL,
        value,
    )


def _warn_status_text_pairing(raw_status: str | None, status: ParcelStatus) -> None:
    """Log every distinct canonical ``raw_status`` once, paired with the inferred status.

    ``raw_status`` is ``latestStatusText``, falling back to the newest
    ``deliveryEvents[].description`` when that's empty (germany.md's
    canonical mapping — the fallback is not optional). §6: "this is the one
    that completes §5" — the only mechanism that will ever build a real DE
    status vocabulary, since no in-transit capture exists yet.
    """
    key = repr(raw_status)
    if key in _status_text_pairs_logged:
        return
    _status_text_pairs_logged.add(key)
    _LOGGER.warning(
        "GLS Germany raw_status/status pairing seen for the first "
        "time — please tell us what the GLS app showed at this moment. "
        "Open an issue: %s\n  raw_status=%s → inferred status=%s",
        _NEW_ISSUE_URL,
        key,
        status,
    )


_KNOWN_TOP_LEVEL_KEYS = {
    # Captured on the wire 2026-08-10 (germany.md):
    "id",
    "trackingReference",
    "parcelNumber",
    "name",
    "parcelIcon",
    "deliveryDirection",
    "deliveredAt",
    "latestStatusText",
    "unlocked",
    "archived",
    "hasDeliveryAttemptFailed",
    "updatedAt",
    "addedAt",
    "deliveryEvents",
    # Dex-reconstructed, mostly never captured — still "known" (expected once
    # `unlocked`/in-transit, per germany.md), so these do not warn.
    # `isInternational` is the exception: captured 2026-08-16 alongside
    # `international` (both `false`) — a real key after all, just not the
    # one the 2026-08-11 signal was about.
    "trackId",
    "isInternational",
    "additionalIds",
    "realTimeTrackingInformation",
    "depotInformation",
    "shopInformation",
    "consigneeInformation",
    "senderInformation",
    # Reported via this WARNING itself (ha-gls#2, 2026-08-11/13) — key names
    # and types only, no captured values (germany.md's "Fourth signal" note).
    # `international` is the real name the dex guess `isInternational` above
    # got wrong. `recipientName` is mapped into `receiver` (see
    # `_receiver_name`); the rest are silenced only to stop the per-restart
    # noise on a known-real DE parcel, still unmapped:
    "failedDeliveryAttempt",
    "isEvDelivery",
    "recipientName",
    "pickedUpFromDepot",
    "international",
    # Fifth real capture, 2026-08-16 (a full delivered payload, not just a
    # WARNING-log line): confirmed `isInternational` co-occurs with
    # `international` as its own distinct key (both `false`) — the "wrong
    # dex name" note above is corrected to "an *additional* real key", not a
    # replacement. Also surfaced one genuinely new field, still only ever
    # seen `null`:
    "changeDeliveryUrl",
}


def _warn_unexpected_top_level_keys(raw: dict) -> None:
    """One-shot-per-key warning for a ``TrackingDetailsDto`` key outside the known set (§6).

    This is how the status enum's own field name — never observed on the
    wire — would eventually be discovered: it would show up here first.
    ``path: type`` lines only, safe to paste into a public issue.
    """
    for key, value in raw.items():
        if key in _KNOWN_TOP_LEVEL_KEYS or key in _unexpected_keys_logged:
            continue
        _unexpected_keys_logged.add(key)
        _LOGGER.warning(
            "GLS Germany response has an unrecognised top-level field — "
            "help us map it. Open an issue and paste this line: %s\n"
            "  %s: %s",
            _NEW_ISSUE_URL,
            key,
            type(value).__name__,
        )


def _warn_time_frame_type(value: Any) -> None:
    """Log ``realTimeTrackingInformation.timeFrame``'s first-ever value/type once (§6).

    Decides whether DE has an ETA at all — BUILD_PLAN_DE.md §4 explicitly
    forbids inventing a window from a prose string, so this is observational
    only, never parsed into ``planned_from``/``planned_to``.
    """
    global _time_frame_type_logged
    if _time_frame_type_logged:
        return
    _time_frame_type_logged = True
    _LOGGER.warning(
        "GLS Germany returned a realTimeTrackingInformation.timeFrame for "
        "the first time — please tell us what it looked like. Open an "
        "issue: %s\n  type=%s value=%r",
        _NEW_ISSUE_URL,
        type(value).__name__,
        value,
    )


def _map_delivered_to_variant(value: str) -> ParcelStatus:
    """``DELIVERED_TO_*`` → ``delivered``, except a shop variant → ``at_pickup_point``.

    The exact member names were never fully recovered (§5) — match by
    whether the member mentions a shop/pickup point or a neighbour/deposit
    handover, and warn on one that fits neither so it gets reported. A
    `DELIVERED_TO_*` prefix always means *some* handover happened, so an
    unrecognised member still defaults to `delivered` rather than `unknown`.
    """
    if any(hint in value for hint in _SHOP_HINTS):
        return ParcelStatus.AT_PICKUP_POINT
    if any(hint in value for hint in _HANDOVER_HINTS):
        return ParcelStatus.DELIVERED
    _warn_unmapped_delivered_to(value)
    return ParcelStatus.DELIVERED


def _map_enum_de(value: str) -> ParcelStatus | None:
    """Map one DE status enum value (§5's dex-reconstructed table)."""
    mapped = _ENUM_MAP.get(value)
    if mapped is not None:
        return mapped
    if value.startswith("DELIVERED_TO_"):
        return _map_delivered_to_variant(value)
    _warn_unmapped_enum(value)
    return None


def map_parcel_status_de(
    enum_value: str | None,
    *,
    delivered_at: str | None,
    has_delivery_attempt_failed: bool,
) -> ParcelStatus:
    """Map a DE parcel to a canonical status — **derivation-first, not enum-first**.

    This is the maintainer's explicit decision to build past BUILD_PLAN_DE.md
    §0's gate: two real delivered parcels carried no status field at all and
    an empty ``latestStatusText`` (germany.md), so the enum table (§5) has
    never been corroborated on the wire. The derivation always decides
    ``delivered``/``problem`` first; ``enum_value`` is only consulted — as an
    override layered on top — when the derivation would otherwise be
    ``unknown``, filling in finer in-transit states the two booleans below
    can't express::

        delivered  <- delivered_at is not None
        problem    <- has_delivery_attempt_failed is True
        unknown    <- otherwise (enum_value consulted here only)
    """
    if delivered_at is not None:
        return ParcelStatus.DELIVERED
    if has_delivery_attempt_failed:
        return ParcelStatus.PROBLEM
    if enum_value:
        mapped = _map_enum_de(enum_value)
        if mapped is not None:
            return mapped
    return ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Payload mapping (BUILD_PLAN_DE.md §4)
# ---------------------------------------------------------------------------


def _parse_de_timestamp(value: str | None) -> str | None:
    """Parse a DE ``"%Y-%m-%d %H:%M:%S"`` timestamp into a canonical ISO string.

    DE timestamps carry no timezone (germany.md: a space, not a ``T``) —
    treated as UTC, the same convention NL's naive scan timestamps use, so
    the generic list helpers (``sort_parcels_by_ts``, the delivered-retention
    filter) can parse the result. Never hand the raw string to a naive ISO
    parser — it isn't one.
    """
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return dt.isoformat()


def _sender_name(raw: dict) -> str | None:
    """Best-effort sender name — ``senderInformation``'s sub-shape is unverified (§4)."""
    info = raw.get("senderInformation")
    if isinstance(info, dict):
        name = info.get("name")
        if name:
            return name
    real_time = raw.get("realTimeTrackingInformation")
    if isinstance(real_time, dict):
        return real_time.get("senderName")
    return None


def _receiver_name(raw: dict) -> str | None:
    """Best-effort receiver name.

    ``recipientName`` is a confirmed real top-level key (ha-gls#2,
    2026-08-11/13 WARNING log) — only ever seen ``null`` so far, but a flat
    string field. Prefer it over ``consigneeInformation``, whose nested shape
    is still dex-only and has never once appeared on the wire (§4).
    """
    name = raw.get("recipientName")
    if name:
        return name
    info = raw.get("consigneeInformation")
    if isinstance(info, dict):
        return info.get("name")
    return None


def _pickup(raw: dict) -> tuple[bool, str | None]:
    """Best-effort pickup / pickup_point from ``shopInformation`` (§4, unverified sub-shape)."""
    shop = raw.get("shopInformation")
    if isinstance(shop, dict):
        return True, shop.get("name")
    if isinstance(shop, str) and shop:
        return True, shop
    return False, None


def _build_history_de(
    events: list[dict], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` from DE's ``deliveryEvents`` (already oldest-first).

    ``status`` is intentionally always ``None`` — BUILD_PLAN_DE.md §5:
    "Do not infer at_pickup_point from event description text"; it is
    localized by ``Accept-Language`` and would break the moment the locale
    changes. ``raw_status`` is the event's own ``description``.
    """
    entries: list[dict] = []
    for event in events:
        timestamp = _parse_de_timestamp(event.get("occurrenceDateTime"))
        if timestamp is None:
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "status": None,
                "raw_status": event.get("description"),
            }
        )
    return entries[-max_events:]


def normalize_parcel_de(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "DE",
    include_history: bool = False,
) -> dict:
    """Return a carrier-agnostic parcel dict for a DE ``TrackingDetailsDto``.

    ``weight``/``dimensions`` are always ``None`` — not in the DTO (captured,
    confirmed absent — a visible regression from NL, named in the release
    notes). No top-level field has ever carried the §5 status enum on the
    wire, so ``enum_value`` is always passed as ``None`` here; the parameter
    exists on :func:`map_parcel_status_de` so it's ready the moment
    ``_warn_unexpected_top_level_keys`` below finally surfaces that field's
    real name.
    """
    _warn_unexpected_top_level_keys(raw)

    real_time = raw.get("realTimeTrackingInformation")
    if isinstance(real_time, dict) and "timeFrame" in real_time:
        _warn_time_frame_type(real_time.get("timeFrame"))

    delivered_at = _parse_de_timestamp(raw.get("deliveredAt"))
    has_delivery_attempt_failed = bool(raw.get("hasDeliveryAttemptFailed"))
    status = map_parcel_status_de(
        None,
        delivered_at=delivered_at,
        has_delivery_attempt_failed=has_delivery_attempt_failed,
    )

    # germany.md's canonical mapping: latestStatusText, else the newest
    # deliveryEvents[].description — the fallback is not optional. Two
    # captures had latestStatusText == "" (raw_status came out None with no
    # fallback, a bug); a third real capture (2026-08-11) had it non-empty,
    # which is what exposed the missing fallback.  deliveryEvents is
    # newest-first on the wire at this point (reversal for `history` happens
    # below), so [0] is the newest entry.
    raw_status = raw.get("latestStatusText") or None
    if raw_status is None:
        events_newest_first = raw.get("deliveryEvents") or []
        if events_newest_first:
            raw_status = events_newest_first[0].get("description")
    _warn_status_text_pairing(raw_status, status)

    events = list(raw.get("deliveryEvents") or [])
    events.reverse()  # newest-first on the wire -> oldest-first canonical
    history = _build_history_de(events) if include_history else None

    pickup, pickup_point = _pickup(raw)

    parcel_number = raw.get("parcelNumber") or raw.get("trackingReference")
    url = TRACKING_URL.format(parcel_no=parcel_number) if parcel_number else None

    return {
        "carrier": "GLS",
        "barcode": raw.get("trackingReference"),
        "sender": _sender_name(raw),
        "receiver": _receiver_name(raw),
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered_at is not None,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": url,
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }


__all__ = [
    "GlsDeSession",
    "async_get_parcel_de",
    "map_parcel_status_de",
    "normalize_parcel_de",
]
