"""Tests for the GLS API client."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.gls.api import GlsApiClient, GlsApiError


def _session_returning(status: int, text: str = "") -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _client(session: MagicMock) -> GlsApiClient:
    """Build a client on the Dutch endpoint (host + culture from COUNTRIES)."""
    return GlsApiClient(session, "apm.gls.nl", "nl-NL")


async def test_get_parcel_returns_json_on_200():
    session = _session_returning(200, '{"parcelNo": "123", "state": 4}')
    client = _client(session)
    parcel = await client.async_get_parcel("123", "1234 AB")
    assert parcel["state"] == 4
    # host + normalised postcode (space stripped) end up in the URL
    url = session.get.call_args[0][0]
    assert "apm.gls.nl" in url
    assert "1234AB" in url


async def test_get_parcel_returns_none_on_204():
    client = _client(_session_returning(204))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_returns_none_on_empty_body():
    client = _client(_session_returning(200, ""))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_returns_none_on_unparseable_body():
    client = _client(_session_returning(200, "not json"))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_raises_on_error_status():
    client = _client(_session_returning(500))
    with pytest.raises(GlsApiError):
        await client.async_get_parcel("123", "1234AB")


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = _client(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel("123", "1234AB")
