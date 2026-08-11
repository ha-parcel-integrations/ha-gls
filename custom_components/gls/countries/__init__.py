"""Per-country GLS logic: transport, ``normalize_parcel_<code>``, status maps.

Concern-level modules (``api.py``, ``parcels.py``, ``coordinator.py``,
``config_flow.py``, ``diagnostics.py``) stay top-level and dispatch into a
country module by ``CONF_COUNTRY``; they carry no per-country branching
themselves. A country gets its own module here once its transport, payload
shape or status vocabulary structurally diverges from NL — not merely because
a second country exists. See ``ha-gls``'s own ``CLAUDE.md`` for the trigger
and ``carrier-research/api/gls/BUILD_PLAN_DE.md`` for the decision that
introduced this package.
"""
