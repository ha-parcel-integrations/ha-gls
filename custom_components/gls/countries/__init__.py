"""Per-country GLS logic: transport, ``normalize_parcel_<code>``, status maps.

Concern-level modules (``api.py``, ``parcels.py``, ``coordinator.py``,
``config_flow.py``, ``diagnostics.py``) stay top-level and dispatch into a
country module by ``CONF_COUNTRY``; they carry no per-country branching
themselves. Every country is a package here (``countries/<code>/__init__.py``).
A country gets an *extra* submodule beyond its ``__init__.py`` (e.g. DE's
``session.py``) once its transport, payload shape or status vocabulary
structurally diverges from NL — not merely because a second country exists.
See ``ha-gls``'s own ``CLAUDE.md`` for the trigger and
``carrier-research/api/gls/BUILD_PLAN_DE.md`` for the decision that
introduced this package.
"""
