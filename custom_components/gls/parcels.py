"""Per-country ``normalize_parcel`` dispatcher, plus generic list helpers.

The mapping/status logic itself moved to ``countries/nl/`` and
``countries/de/`` as part of the ``countries/`` restructure — this file picks
the right one by ``country`` and carries no per-country branching beyond that
one dispatch. ``sort_parcels_by_ts`` and the delivered-retention filter stay
here: they operate on already-normalized canonical dicts and are identical
regardless of which country produced them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
)
from .countries.cz import normalize_parcel_cz
from .countries.de import normalize_parcel_de
from .countries.nl import normalize_parcel_nl
from .timeutils import parse_iso as _parse_iso

_NORMALIZERS = {
    "DE": normalize_parcel_de,
}


def normalize_parcel(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = DEFAULT_COUNTRY,
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Dispatch to the right country's ``normalize_parcel_<code>``.

    NL is the default/fallback — a country without its own entry here is
    treated as NL. CZ is special-cased rather than added to
    ``_NORMALIZERS`` because it is the only normalizer that needs
    ``parcel_no`` (the AWB the user entered — CZ's own ``barcode`` source,
    see ``countries/cz``'s docstring); adding it to NL's/DE's signature just
    to keep one dict uniform would touch two normalizers, and their tests,
    for a parameter neither uses.
    """
    if country == "CZ":
        return normalize_parcel_cz(
            raw,
            postal_code=postal_code,
            country=country,
            include_history=include_history,
            parcel_no=parcel_no,
        )
    normalizer = _NORMALIZERS.get(country, normalize_parcel_nl)
    return normalizer(
        raw,
        postal_code=postal_code,
        country=country,
        include_history=include_history,
    )


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
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            p
            for p in parcels
            if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
        ]
    return parcels[:amount]
