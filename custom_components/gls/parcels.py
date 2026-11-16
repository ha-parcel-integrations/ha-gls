"""Generic list helpers shared by every source.

``sort_parcels_by_ts`` and the delivered-retention filter operate on
already-normalized canonical dicts and are identical regardless of whether a
tracked parcel number or an account feed produced them. The per-country
``normalize_parcel`` dispatch for tracked parcels lives in
``tracking/parcels.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
)
from .timeutils import parse_iso as _parse_iso


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalized parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        dt = _parse_iso(parcel.get(key_field))
        if dt is None:
            without_ts.append(parcel)
        else:
            with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


def _apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the configured retention option.

    ``parcels`` is already sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept); the
    ``parcels`` type keeps the N most recent. The parcels stay *tracked*
    either way — this only controls what the delivered sensor shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            p
            for p in parcels
            if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
        ]
    return parcels[:amount]
