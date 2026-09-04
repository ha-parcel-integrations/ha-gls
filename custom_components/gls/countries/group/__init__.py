"""GLS group-leaf countries: the pan-EU transport, payload mapping and status map.

Serves every ``COUNTRIES`` row whose numbering lives in the pan-EU group
tracking index (``rstt028``/``rstt029``), rather
than a national backend: CZ (1.5.0), then AT/IE/FR/SI/HR/IT. Nothing below
is hardcoded to any one
country: ``host``/``group_locale``/the tracking-url template/the preferred
``type=`` value all come from ``COUNTRIES[country]``, exactly like NL's
``host``/``culture``. A later group-leaf country is meant to be one more
``COUNTRIES`` row pointing at this same package, not a copy of it.

Packaged as ``countries/group/`` (not a flat ``countries/group.py``) for
structural symmetry with ``countries/de/`` — this transport needs no
lifecycle submodule of its own (keyless, no session), same as NL.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from ...const import (
    COUNTRIES,
    GLS_GROUP_RSTT028_URL,
    GLS_GROUP_RSTT029_URL,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    GlsApiError,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)


class GlsGroupMaintenanceError(GlsApiError):
    """Raised when a GLS group leaf serves its maintenance page instead of JSON.

    Both group hosts can go down together and answer every path — including
    one known to return real data — with an ``HTTP/1.0 200 OK``
    ``text/html`` "Maintenance" page. Subclassing
    :class:`~...const.GlsApiError` means ``coordinator.py``'s existing
    ``except (GlsApiError, aiohttp.ClientError)`` branch treats this exactly
    like any other transient failure — cached data kept, retried next poll —
    without ever surfacing it as an unmapped status or a parse failure.
    """

    def __init__(self) -> None:
        """Set a message describing the outage rather than a bare status code."""
        self.status_code = 200
        Exception.__init__(
            self, "GLS group leaf served a maintenance page (non-JSON 200 body)"
        )


# ---------------------------------------------------------------------------
# One-shot WARNING state
# ---------------------------------------------------------------------------

_outage_warned = False
_postcode_mismatch_warned: set[str] = set()
_unmapped_status_logged: set[str] = set()
_unmapped_event_logged: set[str] = set()
_unexpected_keys_logged: set[str] = set()
_history_order_warned = False
_unparseable_timestamp_warned = False
_weight_format_logged: set[str] = set()
_unexpected_info_type_logged: set[str] = set()
# The three additions for the six countries shipped past their research gate
# — country-agnostic, same as the ones
# above, so CZ gets them for free too.
_e800_warned: set[str] = set()
_type_retry_warned: set[str] = set()
_unexpected_5xx_warned = False


def _warn_outage_once() -> None:
    """One-shot: the first time a group leaf answers with its maintenance page."""
    global _outage_warned
    if _outage_warned:
        return
    _outage_warned = True
    _LOGGER.warning(
        "GLS's group tracking service appears to be down (a non-JSON "
        "response on a 200) — this is GLS, not the integration. Cached "
        "parcel data will keep showing until it recovers. Open an issue if "
        "it doesn't clear on its own: %s",
        _NEW_ISSUE_URL,
    )


def _warn_postcode_mismatch_once(parcel_no: str) -> None:
    """One-shot per parcel: an E609 means the stored postcode doesn't match this AWB."""
    if parcel_no in _postcode_mismatch_warned:
        return
    _postcode_mismatch_warned.add(parcel_no)
    _LOGGER.warning(
        "GLS reports the postal code for parcel %s does not match its "
        "delivery address — status will still update, but history and "
        "weight need the correct postcode to show. Open an issue if you "
        "believe the postcode is right: %s",
        parcel_no,
        _NEW_ISSUE_URL,
    )


def _warn_e800_once(parcel_no: str) -> None:
    """One-shot per parcel: an E800 on rstt028 for a parcel that may still be live.

    E800 usually means "this leaf does not carry the consignment", but one
    Spanish code proved that isn't always true —
    this is the mechanism that would justify widening the E609-only retry
    trigger, so it stays a WARNING rather than silence.
    """
    if parcel_no in _e800_warned:
        return
    _e800_warned.add(parcel_no)
    _LOGGER.warning(
        "GLS's group leaf does not carry parcel %s — if this parcel is "
        "genuinely on its way, please open an issue so we can check whether "
        "a different lookup is needed: %s",
        parcel_no,
        _NEW_ISSUE_URL,
    )


def _warn_type_retry_once(parcel_no: str) -> None:
    """One-shot per parcel: the empty-``type=`` call failed and ``type=NAT`` resolved it.

    The only way to learn whether the ``NAT`` requirement is a code-format
    or a per-country thing.
    """
    if parcel_no in _type_retry_warned:
        return
    _type_retry_warned.add(parcel_no)
    _LOGGER.warning(
        "GLS's group leaf only resolved parcel %s after retrying with a "
        "different lookup type — this helps us understand when that retry "
        "is needed. Open an issue and mention this: %s",
        parcel_no,
        _NEW_ISSUE_URL,
    )


def _warn_unexpected_5xx_once() -> None:
    """One-shot: the first ``5xx`` seen from either group leaf."""
    global _unexpected_5xx_warned
    if _unexpected_5xx_warned:
        return
    _unexpected_5xx_warned = True
    _LOGGER.warning(
        "GLS's group tracking service returned an unexpected server error — "
        "this is GLS, not the integration. Open an issue if it keeps "
        "happening: %s",
        _NEW_ISSUE_URL,
    )


def _warn_unmapped_status_once(value: str) -> None:
    """One-shot per distinct ``statusInfo`` value outside the status map."""
    if value in _unmapped_status_logged:
        return
    _unmapped_status_logged.add(value)
    _LOGGER.warning(
        "Unrecognised GLS status — help us map it. Open an issue and paste "
        "this line: %s\n  statusInfo=%s → reported as 'unknown'",
        _NEW_ISSUE_URL,
        value,
    )


def _warn_unmapped_event_once(evt_no: str) -> None:
    """One-shot per distinct ``history[].evtNo`` value outside ``_EVENT_STATUS_MAP``."""
    if evt_no in _unmapped_event_logged:
        return
    _unmapped_event_logged.add(evt_no)
    _LOGGER.warning(
        "Unrecognised GLS history event code — help us map it. Open an "
        "issue and paste this line: %s\n  evtNo=%s → reported with no "
        "status",
        _NEW_ISSUE_URL,
        evt_no,
    )


def _warn_unexpected_top_level_keys(raw: dict) -> None:
    """One-shot-per-key warning for a group-leaf body key outside the known set.

    ``path: type`` lines only, safe to paste into a public issue — same
    convention as ``countries/de``'s equivalent.
    """
    for key, value in raw.items():
        if key in _KNOWN_TOP_LEVEL_KEYS or key in _unexpected_keys_logged:
            continue
        _unexpected_keys_logged.add(key)
        _LOGGER.warning(
            "GLS group-leaf response has an unrecognised top-level field — "
            "help us map it. Open an issue and paste this line: %s\n  "
            "%s: %s",
            _NEW_ISSUE_URL,
            key,
            type(value).__name__,
        )


def _warn_history_order_once() -> None:
    """One-shot: the first body whose ``history[]`` isn't newest-first on the wire."""
    global _history_order_warned
    if _history_order_warned:
        return
    _history_order_warned = True
    _LOGGER.warning(
        "GLS returned a parcel history that is not newest-first — the "
        "reversal this integration relies on may be wrong for this "
        "parcel. Open an issue: %s",
        _NEW_ISSUE_URL,
    )


def _warn_unparseable_timestamp_once(date_value: Any, time_value: Any) -> None:
    """One-shot: the first ``date``/``time`` pair that fails to parse."""
    global _unparseable_timestamp_warned
    if _unparseable_timestamp_warned:
        return
    _unparseable_timestamp_warned = True
    _LOGGER.warning(
        "GLS returned a date/time that could not be parsed — please tell "
        "us what it looked like. Open an issue: %s\n  date=%r time=%r",
        _NEW_ISSUE_URL,
        date_value,
        time_value,
    )


def _warn_weight_format_once(value: Any) -> None:
    """One-shot per distinct malformed ``infos[]`` WEIGHT value."""
    key = repr(value)
    if key in _weight_format_logged:
        return
    _weight_format_logged.add(key)
    _LOGGER.warning(
        "GLS returned a weight value in an unexpected format — please "
        "tell us what it looked like. Open an issue: %s\n  value=%r",
        _NEW_ISSUE_URL,
        value,
    )


def _warn_unexpected_info_type_once(info_type: Any) -> None:
    """One-shot per distinct ``infos[].type`` other than ``WEIGHT``."""
    key = repr(info_type)
    if key in _unexpected_info_type_logged:
        return
    _unexpected_info_type_logged.add(key)
    _LOGGER.warning(
        "GLS returned an infos[] entry of an unrecognised type "
        "(dimensions, maybe?) — help us map it. Open an issue: %s\n  "
        "type=%r",
        _NEW_ISSUE_URL,
        info_type,
    )


# ---------------------------------------------------------------------------
# Transport — rstt028 primary, rstt029 fallback on a postcode mismatch
# ---------------------------------------------------------------------------


def _parse_json_or_none(text: str) -> dict[str, Any] | None:
    """Parse a response body as JSON, or ``None`` when it isn't JSON at all.

    The group leaves' maintenance page is an HTML ``200`` — indistinguishable
    from a real response on the status line alone, so JSON-parseability (not
    the status code) is what decides "outage" here.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _async_get_json(
    session: aiohttp.ClientSession, url: str
) -> tuple[dict[str, Any] | None, int]:
    """One GET, returning the parsed JSON body (or ``None``) and the HTTP status."""
    async with session.get(url) as response:
        status = response.status
        text = await response.text()
    return _parse_json_or_none(text), status


async def _async_get_parcel_group_fallback(
    session: aiohttp.ClientSession,
    host: str,
    group_locale: str,
    parcel_no: str,
    country: str,
) -> dict[str, Any] | None:
    """``rstt029`` — the AWB-only fallback used on an ``E609`` postcode mismatch.

    Returns the same shape ``rstt028`` uses for ``progressBar`` (so status
    mapping is unaffected) but with no ``history``/``infos``/``references`` —
    ``normalize_parcel_group`` reports those as absent exactly as it would
    for any body missing those keys.

    ``type=`` is retried once: most codes
    resolve on the first value tried, but a non-plain-numeric AWB can need
    the other one — an ``HTTP 5xx`` or a ``404 lastError E206`` is the
    trigger, not a per-country prediction. ``COUNTRIES[country]["group_type"]``
    (default ``""``) picks which value goes first; Italy sets it to
    ``"NAT"`` so it stops paying for a wasted ``500`` round trip on every
    poll. Two attempts at most either way.
    """
    preferred_type = COUNTRIES.get(country, {}).get("group_type", "")
    other_type = "" if preferred_type == "NAT" else "NAT"
    type_candidates = [preferred_type, other_type]

    for attempt, type_value in enumerate(type_candidates):
        url = GLS_GROUP_RSTT029_URL.format(
            host=host,
            group_locale=group_locale,
            awb=parcel_no,
            type=type_value,
            millis=int(time.time() * 1000),
        )
        body, status = await _async_get_json(session, url)
        if body is None:
            _warn_outage_once()
            raise GlsGroupMaintenanceError()

        last_error = body.get("lastError")
        is_5xx = status >= 500
        needs_retry = is_5xx or (status == 404 and last_error == "E206")
        has_more_candidates = attempt < len(type_candidates) - 1

        if needs_retry:
            if is_5xx:
                _warn_unexpected_5xx_once()
            if has_more_candidates:
                continue
            if is_5xx:
                # Both candidates faulted — a server-side problem, not a
                # "not found", so the coordinator's transient handling
                # (cached data kept, retried next poll) is the right home.
                raise GlsApiError(status)
            return None  # E206 on the last candidate: a structural rejection

        if last_error:
            return None
        tu_status = body.get("tuStatus") or []
        if not tu_status:
            return None
        if attempt > 0:
            _warn_type_retry_once(parcel_no)
        return dict(tu_status[0])
    return None  # pragma: no cover - loop always returns/raises above


async def async_get_parcel_group(
    session: aiohttp.ClientSession,
    host: str,
    group_locale: str,
    parcel_no: str,
    postal_code: str,
    *,
    country: str = "CZ",
) -> dict[str, Any] | None:
    """Fetch one group-leaf parcel's tracking details.

    ``rstt028`` is the only call per poll in the common case — it carries
    everything ``rstt029`` does plus history, weight and references. Falls
    back to ``rstt029`` only on an ``E609`` (AWB right, postcode wrong), so
    a parcel with a stale stored postcode still shows its status. ``E206``
    (malformed reference) reports as ``None``, the same "not found" signal
    NL's ``204`` uses. ``E800`` (AWB not in the group index) also reports as
    ``None`` but fires a one-shot WARNING first — it usually means absence,
    but not always, and that WARNING is the
    mechanism that would justify widening the retry trigger one day. Any
    other non-2xx status raises
    :class:`~...const.GlsApiError`; a non-JSON body raises
    :class:`GlsGroupMaintenanceError`.
    """
    url = GLS_GROUP_RSTT028_URL.format(
        host=host,
        group_locale=group_locale,
        awb=parcel_no,
        millis=int(time.time() * 1000),
        postal_code=postal_code.replace(" ", ""),
    )
    body, status = await _async_get_json(session, url)
    if body is None:
        _warn_outage_once()
        raise GlsGroupMaintenanceError()

    last_error = body.get("lastError")
    if last_error == "E609":
        _warn_postcode_mismatch_once(parcel_no)
        return await _async_get_parcel_group_fallback(
            session, host, group_locale, parcel_no, country
        )
    if last_error == "E800":
        _warn_e800_once(parcel_no)
        return None
    if last_error:
        # E206 (malformed reference) — a semantic "we don't have this", not
        # a postcode problem.
        return None
    if status != 200:
        if status >= 500:
            _warn_unexpected_5xx_once()
        raise GlsApiError(status)
    return body


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, ParcelStatus] = {
    "PREADVICE": ParcelStatus.REGISTERED,
    "PROCESSING": ParcelStatus.REGISTERED,
    "INTRANSIT": ParcelStatus.IN_TRANSIT,
    "INWAREHOUSE": ParcelStatus.IN_TRANSIT,
    "INPICKUP": ParcelStatus.IN_TRANSIT,
    "MULTIPACK": ParcelStatus.IN_TRANSIT,
    "INDELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVEREDPS": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "NOTPICKEDUP": ParcelStatus.RETURNING,
    "RETURNED": ParcelStatus.RETURNING,
    "NOTDELIVERED": ParcelStatus.PROBLEM,
    "CANCELLED": ParcelStatus.PROBLEM,
    "CANCELED": ParcelStatus.PROBLEM,
    "UNAVAILABLE": ParcelStatus.PROBLEM,
}


def map_parcel_status_group(
    status_info: str | None, *, retour_flag: bool = False
) -> ParcelStatus:
    """Map ``progressBar.statusInfo`` to a canonical status — this package's own function.

    Kept entirely separate from NL's numeric ``_STATE_MAP`` and DE's
    derivation-based path — a different mechanism, a different vocabulary,
    no shared dict. Country-independent: the sweep found no shape
    difference across CZ/AT/IE/FR/SI/HR/IT.

    ``retour_flag`` (``progressBar.retourFlag``) overrides to ``returning``
    unconditionally, whatever ``status_info`` says — checked first here,
    which is equivalent to "after the table" since it always wins either way.

    ``DELIVEREDPS`` contains the substring ``DELIVERED``; the dict lookup
    below is an exact match, never ``in``/``startswith``, so a ParcelShop
    arrival is never silently reported as delivered.
    """
    if retour_flag:
        return ParcelStatus.RETURNING
    if not status_info:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(status_info)
    if mapped is not None:
        return mapped
    _warn_unmapped_status_once(status_info)
    return ParcelStatus.UNKNOWN


# ``history[].evtNo`` — an observed subset, not a documented enum (nothing
# published enumerates the full code space), so this maps only what's been
# seen on a real capture and reports anything else with a one-shot warning,
# the same convention DHL's ``map_event_status`` uses. ``3.896``'s ``evtDscr``
# ("The parcel has been delivered into the ParcelLocker.") unambiguously
# describes *that event* as a pickup-point moment, so it maps here even
# though whether a parcel *sitting* in a locker reports parcel-level
# ``at_pickup_point`` is a separate, still-open question that this per-event
# map does not answer — ``map_parcel_status_group`` (parcel-level, keyed on
# ``progressBar.statusInfo``) is untouched.
_EVENT_STATUS_MAP: dict[str, ParcelStatus] = {
    "0.100": ParcelStatus.REGISTERED,    # parcel data entered, not yet handed over
    "0.0": ParcelStatus.IN_TRANSIT,      # handed over to GLS
    "2.0": ParcelStatus.IN_TRANSIT,      # reached a parcel center
    "11.0": ParcelStatus.OUT_FOR_DELIVERY,
    "17.0": ParcelStatus.IN_TRANSIT,     # picked up from the sender by the courier
    "3.0": ParcelStatus.DELIVERED,
    "3.896": ParcelStatus.AT_PICKUP_POINT,  # deposited into a ParcelLocker
}


def map_event_status_group(evt_no: str | None) -> ParcelStatus | None:
    """Map one ``history[].evtNo`` to a canonical status, or ``None`` if unmapped.

    Unlike :func:`map_parcel_status_group`, an unmapped code reports as
    ``None`` (the history entry keeps ``status: null``) rather than falling
    back to ``ParcelStatus.UNKNOWN`` — mirroring DHL's per-event convention,
    since a single parcel's status is a required field but any one history
    entry's status is inherently optional.
    """
    if not evt_no:
        return None
    mapped = _EVENT_STATUS_MAP.get(evt_no)
    if mapped is not None:
        return mapped
    _warn_unmapped_event_once(evt_no)
    return None


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL_KEYS = {
    # rstt028's own shape:
    "tuNo",
    "referenceNo",
    "date",
    "postalCode",
    "status",
    "natSysOwnerCode",
    "deliveryOwnerCode",
    "owners",
    "emailNotificationCard",
    "signature",
    "infos",
    "references",
    "progressBar",
    "history",
    # rstt029's tuStatus[] entry shape (the E609 fallback body) — a subset,
    # plus arrivalTime in place of the fields rstt028-only carries:
    "arrivalTime",
}

_WEIGHT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z]+)\s*$")


def _parse_group_datetime(date_value: Any, time_value: Any) -> str | None:
    """Parse a ``date`` + ``time`` pair into a canonical (naive) ISO string.

    Defensive against an unconfirmed ``YY-MM-DD`` variant existing alongside
    the ``YYYY-MM-DD`` seen on the captured CZ parcel —
    tries both, warns once on total failure rather than raising.
    """
    if not date_value or not time_value:
        return None
    for date_fmt in ("%Y-%m-%d", "%y-%m-%d"):
        try:
            parsed = datetime.strptime(f"{date_value} {time_value}", f"{date_fmt} %H:%M:%S")
        except ValueError:
            continue
        return parsed.isoformat()
    _warn_unparseable_timestamp_once(date_value, time_value)
    return None


def _check_history_order(parsed_timestamps: list[str]) -> None:
    """Warn once if consecutive parsed timestamps aren't newest-first on the wire."""
    for earlier, later in zip(parsed_timestamps, parsed_timestamps[1:]):
        if later > earlier:
            _warn_history_order_once()
            return


def _build_history_group(
    raw_history: list[dict], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` from ``history[]`` (wire newest-first).

    ``status`` is derived per event from ``evtNo`` via
    :func:`map_event_status_group` — the same "map what's been seen, warn on
    the rest" convention DHL's history uses. ``evtNo`` repeats across
    entries, so it is never used as a dedup key either — every entry the
    wire sends is kept.
    """
    parsed_timestamps: list[str] = []
    canonical: list[dict] = []
    for event in raw_history:
        timestamp = _parse_group_datetime(event.get("date"), event.get("time"))
        if timestamp is not None:
            parsed_timestamps.append(timestamp)
        raw_status = event.get("evtDscr")
        canonical.append(
            {
                "timestamp": timestamp,
                "status": map_event_status_group(event.get("evtNo")),
                "raw_status": html.unescape(raw_status) if raw_status else None,
            }
        )
    _check_history_order(parsed_timestamps)
    canonical.reverse()  # wire newest-first -> canonical oldest-first
    return canonical[-max_events:]


def _extract_weight_kg(infos: list[dict] | None) -> float | None:
    """Return the parsed ``kg`` weight from ``infos[]``, or ``None``.

    The value is a unit-suffixed display string (``"0.9 kg"``), not a
    number — the numeric part is parsed and the ``kg`` suffix is asserted
    rather than assumed. Any ``infos[]`` entry whose ``type`` isn't
    ``WEIGHT`` (dimensions might show up here one day) is reported, never
    silently skipped.
    """
    weight_kg: float | None = None
    for info in infos or []:
        info_type = info.get("type")
        if info_type != "WEIGHT":
            _warn_unexpected_info_type_once(info_type)
            continue
        value = info.get("value")
        match = _WEIGHT_RE.match(value) if isinstance(value, str) else None
        if not match:
            _warn_weight_format_once(value)
            continue
        number, unit = match.groups()
        if unit.lower() != "kg":
            _warn_weight_format_once(value)
            continue
        try:
            weight_kg = float(number.replace(",", "."))
        except ValueError:
            _warn_weight_format_once(value)
    return weight_kg


def _tracking_url_group(parcel_no: str | None, country: str) -> str | None:
    """Return the consumer tracking deep-link, from ``COUNTRIES[country]``."""
    if not parcel_no:
        return None
    template = COUNTRIES.get(country, {}).get("tracking_url") or TRACKING_URL
    return template.format(parcel_no=parcel_no)


def normalize_parcel_group(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = "CZ",
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Return a carrier-agnostic parcel dict for a GLS group-leaf body.

    ``parcel_no`` is the AWB the caller looked up (threaded from
    ``coordinator.py`` through ``parcels.py``'s dispatcher) and is what
    ``barcode`` is built from — never trusted from the response's own
    ``tuNo``/``referenceNo``. Those happened to equal the AWB for the
    captured Czech consignment but are an unrelated identifier for a Danish
    one (and a space-inserted variant for an Italian one) on the same leaves,
    so they are only a last-resort fallback here, for a caller that has no
    AWB of its own to pass.

    ``sender``/``receiver``/``pickup_point``/``planned_from``/``planned_to``/
    ``dimensions`` are always ``None`` — neither group leaf carries them.
    ``weight`` is populated (unlike DE) from ``infos[]``. Top-level ``date``
    is the consignment date, never used for ``delivered_at`` — that comes
    from the newest ``history[]`` entry instead.
    """
    _warn_unexpected_top_level_keys(raw)

    progress = raw.get("progressBar") or {}
    status_info = progress.get("statusInfo")
    retour_flag = bool(progress.get("retourFlag"))
    status = map_parcel_status_group(status_info, retour_flag=retour_flag)

    # DELIVEREDPS contains the substring DELIVERED — exact match only.
    delivered = status_info == "DELIVERED"
    is_pickup = status_info == "DELIVEREDPS"

    raw_history = raw.get("history") or []
    newest_event = raw_history[0] if raw_history else None
    delivered_at = None
    if delivered and newest_event:
        delivered_at = _parse_group_datetime(
            newest_event.get("date"), newest_event.get("time")
        )

    history = _build_history_group(raw_history) if include_history else None

    # progressBar.statusText is the progress bar's heading ("Delivered"),
    # not a description of the latest event — it stays "Delivered" once a
    # ParcelLocker drop-off (DELIVEREDPS) is reached, contradicting the
    # canonical at_pickup_point status (ha-gls#6). The newest history[]
    # entry's evtDscr is the precise, per-event wording instead.
    event_status_text = newest_event.get("evtDscr") if newest_event else None
    status_text = event_status_text or progress.get("statusText")
    raw_status = html.unescape(status_text) if status_text else None

    barcode = parcel_no or raw.get("referenceNo") or raw.get("tuNo")

    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": is_pickup,
        "pickup_point": None,
        "url": _tracking_url_group(barcode, country),
        "weight": _extract_weight_kg(raw.get("infos")),
        "dimensions": None,
        "history": history,
        "raw": raw,
    }


__all__ = [
    "GlsGroupMaintenanceError",
    "async_get_parcel_group",
    "map_event_status_group",
    "map_parcel_status_group",
    "normalize_parcel_group",
]
