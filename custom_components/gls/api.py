"""Thin per-country dispatcher for the GLS transport layer.

``GlsApiClient`` is constructed once per config entry (in ``__init__.py``)
and used by the coordinator without it needing to know which country it is
talking to. All the actual HTTP mechanics live in ``countries/<code>.py``;
this class only picks the right one and forwards ``async_get_parcel`` to it.

``GlsApiError`` is defined in ``const.py`` (not here) so a country module can
raise it without an import cycle back through this file — re-exported here so
existing imports (``from .api import GlsApiClient, GlsApiError``) and test
patch targets (``custom_components.gls.api.GlsApiClient.async_get_parcel``)
keep working unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp

from .const import DEFAULT_COUNTRY, GROUP_LEAF_COUNTRIES, GlsApiError
from .countries.de import async_get_parcel_de
from .countries.group import async_get_parcel_group
from .countries.nl import async_get_parcel_nl

if TYPE_CHECKING:
    from .countries.de.session import GlsDeSession

__all__ = ["GlsApiClient", "GlsApiError"]


class GlsApiClient:
    """Dispatches ``async_get_parcel`` to the hub's country transport.

    NL needs only the session, host and culture (a bare keyless ``GET``).
    DE additionally needs a :class:`~.countries.de.session.GlsDeSession` for
    its bearer token — pass it as ``de_session`` when ``country="DE"``.
    Constructing a DE client without one is a configuration bug, not a
    runtime API failure, so it raises ``RuntimeError`` rather than
    ``GlsApiError``. Every ``GROUP_LEAF_COUNTRIES`` member needs
    ``group_locale`` (the group leaf's ``{ISO2}/{lang}`` path segment)
    instead of ``culture`` — see ``const.py``'s ``COUNTRIES`` docstring for
    why the two aren't the same key.

    Wiring a real DE hub through end-to-end (``__init__.py`` constructing
    this with ``country="DE"`` + a live ``GlsDeSession``, and
    ``config_flow.py`` minting/persisting the ``appInstanceId`` that session
    needs) is deliberately **not** part of this change — see
    BUILD_PLAN_DE.md §1's file table for what still touches
    ``coordinator.py``/``config_flow.py``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        culture: str,
        *,
        country: str = DEFAULT_COUNTRY,
        de_session: "GlsDeSession | None" = None,
        group_locale: str | None = None,
    ) -> None:
        """Initialise the client for one hub's country."""
        self._session = session
        self._host = host
        self._culture = culture
        self._country = country
        self._de_session = de_session
        self._group_locale = group_locale

    async def async_get_parcel(
        self, parcel_no: str, postal_code: str
    ) -> dict | None:
        """Fetch one parcel's tracking details via the hub's country transport."""
        if self._country == "DE":
            if self._de_session is None:
                raise RuntimeError(
                    "GlsApiClient built with country='DE' but no de_session"
                )
            return await async_get_parcel_de(
                self._session, self._de_session, parcel_no, postal_code
            )
        if self._country in GROUP_LEAF_COUNTRIES:
            return await async_get_parcel_group(
                self._session,
                self._host,
                self._group_locale,
                parcel_no,
                postal_code,
                country=self._country,
            )
        return await async_get_parcel_nl(
            self._session, self._host, self._culture, parcel_no, postal_code
        )
