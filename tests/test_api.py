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


async def test_get_parcel_returns_json_on_200():
    session = _session_returning(200, '{"parcelNo": "123", "state": 4}')
    client = GlsApiClient(session)
    parcel = await client.async_get_parcel("123", "1234 AB")
    assert parcel["state"] == 4
    # postcode is normalised (space stripped) into the URL
    assert "1234AB" in session.get.call_args[0][0]


async def test_get_parcel_returns_none_on_204():
    client = GlsApiClient(_session_returning(204))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_returns_none_on_empty_body():
    client = GlsApiClient(_session_returning(200, ""))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_returns_none_on_unparseable_body():
    client = GlsApiClient(_session_returning(200, "not json"))
    assert await client.async_get_parcel("123", "1234AB") is None


async def test_get_parcel_raises_on_error_status():
    client = GlsApiClient(_session_returning(500))
    with pytest.raises(GlsApiError):
        await client.async_get_parcel("123", "1234AB")


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = GlsApiClient(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel("123", "1234AB")
