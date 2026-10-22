"""GLS France: resolve the alphanumeric reference into the numeric AWB.

**France is not tracked here.** French parcels stay on the pan-EU group leaf
(``countries/group/``), which carries the status vocabulary, the history and
the mapping this integration already trusts. This package does one narrow job
instead, and it is deliberately not a transport:

GLS France parcels present *two* identifier forms. The numeric AWB resolves on
the group leaves. The 8-character alphanumeric reference — the one a GLS France
notification and a ``moncolis`` deep link actually carry, so the one a French
user is most likely to hold — is rejected by both leaves with ``404 E206`` /
``E800``, because the group index has never held that form. Until now a user
with only that reference could not track their parcel at all.

The national consignee service indexes the alphanumeric form **and returns the
numeric AWB beside it**, so it can be used purely as a translator: reference
in, AWB out, and the AWB then goes to the leaf this integration already drives.

Why a resolver rather than a full French transport:

* it adds coverage without touching France's status mapping, and the national
  surface's own vocabulary is one value wide — only ``LIV`` (delivered) has
  ever been observed, so mapping on it would be a regression risk;
* a plain numeric code never reaches this module, so for every French user who
  already works today **nothing changes and no extra request is made**;
* a failed resolution falls back to trying the code on the leaf exactly as
  before, so this can only add successes.

The shipped ``E609`` → ``rstt029`` fallback is a different thing and does not
overlap: that one covers a *postcode* mismatch, not an identifier form the
group index does not hold.
"""

from __future__ import annotations

import logging

import aiohttp

from ...const import GLS_FR_RESOLVE_URL

_LOGGER = logging.getLogger(__name__)

_ACCEPT = "application/json"

# The reference -> AWB mapping is a property of the shipment and does not
# change, so one resolution per process is enough. Deliberately not persisted,
# unlike DE's learned parcel numbers: re-resolving after a restart costs one
# cheap keyless GET, which is not worth a state file and a migration.
_resolved: dict[str, str] = {}

_failures_logged: set[str] = set()


def needs_resolving(reference: str) -> bool:
    """Whether ``reference`` is a form the group leaves cannot resolve.

    A plain numeric AWB is what the group index holds, so it is passed through
    untouched — that is what keeps every French parcel that works today on
    exactly the path it is on now, with no extra request.
    """
    code = reference.strip() if reference else ""
    return bool(code) and not code.isdigit()


async def async_resolve_reference_fr(
    session: aiohttp.ClientSession, reference: str
) -> str | None:
    """Return the numeric AWB for an alphanumeric French reference.

    ``None`` means "could not translate" for any reason — an unknown
    reference, a transport failure, or a reply without the AWB in it. The
    caller falls back to trying the original code on the group leaf, which is
    the behaviour that shipped before this resolver existed, so a ``None`` can
    never make things worse than they were.
    """
    code = reference.strip()
    cached = _resolved.get(code)
    if cached is not None:
        return cached

    url = GLS_FR_RESOLVE_URL.format(reference=code)
    try:
        async with session.get(url, headers={"Accept": _ACCEPT}) as response:
            status = response.status
            if status == 404:
                # Semantic not-found: the body names the code it was asked
                # for. Nothing to translate, and the group leaf will not hold
                # it either -- but the caller still tries, unchanged.
                return None
            if status != 200:
                _log_failure_once(code, f"HTTP {status}")
                return None
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                _log_failure_once(code, "non-JSON body")
                return None
    except aiohttp.ClientError as err:
        # Never fails the poll: this is an optional translation step in front
        # of the real transport, so a network problem here degrades to the
        # old behaviour rather than dropping the parcel.
        _log_failure_once(code, type(err).__name__)
        return None

    parcel = body.get("colis") if isinstance(body, dict) else None
    if not isinstance(parcel, dict):
        _log_failure_once(code, "no 'colis' object")
        return None

    # Three identifier forms come back in one reply (trackid, numeroGp,
    # numeroalphaColis). Only numeroGp is the group leaves' identifier --
    # picking either of the others would hand the leaf a code it cannot
    # resolve and look exactly like a carrier outage.
    awb = parcel.get("numeroGp")
    if not isinstance(awb, str) or not awb.strip().isdigit():
        _log_failure_once(code, "no numeric numeroGp")
        return None

    awb = awb.strip()
    _resolved[code] = awb
    _LOGGER.debug("Resolved a GLS France reference to its numeric AWB")
    return awb


def _log_failure_once(code: str, reason: str) -> None:
    """Report an unresolvable reference once per process, without the code."""
    if reason in _failures_logged:
        return
    _failures_logged.add(reason)
    _LOGGER.debug(
        "Could not translate a GLS France reference (%s) — falling back to "
        "looking the code up on the group leaf directly",
        reason,
    )


__all__ = ["async_resolve_reference_fr", "needs_resolving"]
