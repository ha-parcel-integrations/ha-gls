"""GLS Italy: the national ``rstt030`` leaf, used for history only.

**Italy is not tracked here.** Italian parcels keep their status from the
pan-EU group leaves (``countries/group/``), which carry
``progressBar.statusInfo`` — a complete, locale-independent machine
vocabulary. This package supplies the one thing those leaves cannot give an
Italian parcel cheaply: its history.

Why the split rather than a migration. On paper ``rstt030`` is the better
Italian route: it returns the whole history with no postcode, where the group
overview carries none at all and history needs ``rstt028`` *plus* the
recipient's postcode, which is unverified for every group country but Czechia.
But ``rstt030``'s ``history[]`` carries **no event code of any kind** — no
``evtNo``, no ``lastError``, no enum, only a localized sentence. Moving Italy
wholesale would therefore trade a complete machine status vocabulary for a
two-sentence wording map, which is a regression on the field that matters most.
Taking status from one leaf and history from the other keeps both.

Its rows need no translation: ``date``/``time``/``evtDscr`` is exactly the
shape ``countries/group/``'s history builder already reads, because this leaf
sits in the same service family. Rows have no ``evtNo``, so each one's
``status`` comes through as ``None`` — which is a supported, neutral state in
that builder, and the honest one here.

The ``en`` locale in the path is load-bearing, exactly as ``Accept-Language``
is on the Spanish surface: it is what makes the sentences stable.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ...const import GLS_IT_RSTT030_URL

_LOGGER = logging.getLogger(__name__)

_ACCEPT = "application/json"

_failures_logged: set[str] = set()


def _log_failure_once(reason: str) -> None:
    """Report an unavailable national history once per process."""
    if reason in _failures_logged:
        return
    _failures_logged.add(reason)
    _LOGGER.debug(
        "GLS Italy's national history is unavailable (%s) — the parcel keeps "
        "its status from the group leaf and whatever history that carried",
        reason,
    )


async def async_get_history_it(
    session: aiohttp.ClientSession, host: str, parcel_no: str
) -> list[dict[str, Any]] | None:
    """Return the wire ``history[]`` rows (newest first) for an Italian parcel.

    ``None`` means "no national history this poll", for any reason — an
    unknown number, a transport failure, or a reply without history in it.
    This call is strictly additive: the caller keeps the group leaf's own
    answer either way, so a ``None`` degrades to the behaviour that shipped
    before this leaf was used and can never drop a parcel.
    """
    # The service returns the number with a space after the depot code and
    # *rejects* the spaced form on input, so it is always sent unspaced.
    awb = parcel_no.replace(" ", "").strip()
    if not awb:
        return None

    url = GLS_IT_RSTT030_URL.format(host=host, awb=awb)
    try:
        async with session.get(url, headers={"Accept": _ACCEPT}) as response:
            status = response.status
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                # An HTML body here is our own malformed request (omitting
                # type=NAT answers 400 + HTML) or a bot wall. Never a parcel.
                _log_failure_once(f"non-JSON body on HTTP {status}")
                return None
    except aiohttp.ClientError as err:
        _log_failure_once(type(err).__name__)
        return None

    if status != 200:
        # This leaf's absence envelope carries no machine code at all — the
        # group leaves answer theirs with lastError E206/E800, this one with
        # a localized exceptionText and nothing else. So absence is matched
        # on the status plus the missing tuStatus, never on the sentence.
        _log_failure_once(f"HTTP {status}")
        return None

    shipments = body.get("tuStatus") if isinstance(body, dict) else None
    if not isinstance(shipments, list) or not shipments:
        _log_failure_once("no tuStatus")
        return None
    shipment = shipments[0]
    if not isinstance(shipment, dict):
        _log_failure_once("unexpected tuStatus shape")
        return None

    history = shipment.get("history")
    if not isinstance(history, list) or not history:
        _log_failure_once("no history rows")
        return None
    return [row for row in history if isinstance(row, dict)]


__all__ = ["async_get_history_it"]
