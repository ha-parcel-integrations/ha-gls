"""Per-country ``normalize_parcel`` dispatcher for tracked parcels.

The mapping/status logic itself lives in ``tracking/countries/<code>/`` — this
file picks the right one by ``country`` and carries no per-country branching
beyond that one dispatch.
"""

from __future__ import annotations

from ..const import DEFAULT_COUNTRY, ES_COUNTRIES, GROUP_LEAF_COUNTRIES
from .countries.ca import normalize_parcel_ca
from .countries.de import normalize_parcel_de
from .countries.es import normalize_parcel_es
from .countries.group import normalize_parcel_group
from .countries.nl import normalize_parcel_nl
from .countries.pl import normalize_parcel_pl
from .countries.us import normalize_parcel_us

_NORMALIZERS = {
    "CA": normalize_parcel_ca,
    "DE": normalize_parcel_de,
    "PL": normalize_parcel_pl,
    "US": normalize_parcel_us,
    **dict.fromkeys(ES_COUNTRIES, normalize_parcel_es),
    **dict.fromkeys(GROUP_LEAF_COUNTRIES, normalize_parcel_group),
}

# Normalizers whose ``barcode`` source is the code the user entered rather than
# a field of the payload, so they take the extra ``parcel_no`` argument. NL's
# and DE's signatures deliberately stay without it.
_NEEDS_PARCEL_NO = (
    frozenset({"CA", "PL", "US"}) | ES_COUNTRIES | frozenset(GROUP_LEAF_COUNTRIES)
)


def normalize_parcel(
    raw: dict,
    *,
    postal_code: str | None = None,
    country: str = DEFAULT_COUNTRY,
    include_history: bool = False,
    parcel_no: str | None = None,
) -> dict:
    """Dispatch to the right country's ``normalize_parcel_<code>``.

    NL is the default/fallback — a country without its own entry in
    ``_NORMALIZERS`` is treated as NL. The group leaves, CA, PL and US additionally
    take ``parcel_no`` (see ``_NEEDS_PARCEL_NO``); NL and DE do not, and
    widening their signatures just to keep one call uniform would touch two
    normalizers, and their tests, for a parameter neither uses.
    """
    normalizer = _NORMALIZERS.get(country, normalize_parcel_nl)
    extra = {"parcel_no": parcel_no} if country in _NEEDS_PARCEL_NO else {}
    return normalizer(
        raw,
        postal_code=postal_code,
        country=country,
        include_history=include_history,
        **extra,
    )
