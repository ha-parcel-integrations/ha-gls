"""GLS public tracking API client."""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .const import PARCEL_DETAILS_URL

_LOGGER = logging.getLogger(__name__)


class GlsApiError(Exception):
    """Raised when a GLS API call returns an unexpected status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"GLS API request failed with status {status_code}")
        self.status_code = status_code


class GlsApiClient:
    """Client for the public GLS parcel tracking endpoint.

    No authentication: the endpoint is keyed on the parcel number + the
    delivery postal code, exactly like the GLS consumer site. The ``host``
    and ``culture`` are the hub country's (see ``COUNTRIES``).
    """

    def __init__(
        self, session: aiohttp.ClientSession, host: str, culture: str
    ) -> None:
        """Initialise the client with an aiohttp session and country endpoint."""
        self._session = session
        self._host = host
        self._culture = culture

    async def async_get_parcel(
        self, parcel_no: str, postal_code: str
    ) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parsed JSON dict for a known parcel, or ``None`` when the
        endpoint answers ``204 No Content`` (unknown or not-yet-scanned
        parcel). Any other non-2xx status raises :class:`GlsApiError`; network
        errors propagate as ``aiohttp.ClientError``.

        The response is served with a ``text/plain`` mimetype, so the body is
        parsed with ``json.loads`` rather than ``response.json()``.
        """
        url = PARCEL_DETAILS_URL.format(
            host=self._host,
            parcel_no=parcel_no,
            postal_code=postal_code.replace(" ", ""),
            culture=self._culture,
        )
        async with self._session.get(url) as response:
            if response.status == 204:
                return None
            if response.status != 200:
                raise GlsApiError(response.status)
            text = await response.text()

        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            _LOGGER.warning("GLS returned an unparseable body for %s: %s", parcel_no, err)
            return None
