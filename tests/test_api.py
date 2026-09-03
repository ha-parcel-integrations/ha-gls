"""Tests for the GLS API client."""
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# Country dispatch — DE not wired end-to-end yet (no __init__.py/config_flow
# construction path exists), but the dispatcher branch itself is unit-tested
# directly here.
# ---------------------------------------------------------------------------


async def test_de_dispatch_without_a_session_is_a_configuration_error():
    client = GlsApiClient(MagicMock(), "unused", "unused", country="DE")
    with pytest.raises(RuntimeError):
        await client.async_get_parcel("075624238061", "00000")


async def test_de_dispatch_delegates_to_the_de_transport():
    de_session = MagicMock()
    client = GlsApiClient(
        MagicMock(), "unused", "unused", country="DE", de_session=de_session
    )
    with patch(
        "custom_components.gls.api.async_get_parcel_de",
        new=AsyncMock(return_value={"parcelNumber": "YOXVB8CE"}),
    ) as mock_transport:
        result = await client.async_get_parcel("075624238061", "00000")
    assert result == {"parcelNumber": "YOXVB8CE"}
    mock_transport.assert_awaited_once_with(
        client._session, de_session, "075624238061", "00000"
    )


async def test_cz_dispatch_delegates_to_the_group_transport():
    client = GlsApiClient(
        MagicMock(),
        "gls-group.com",
        "unused",
        country="CZ",
        group_locale="CZ/en",
    )
    with patch(
        "custom_components.gls.api.async_get_parcel_group",
        new=AsyncMock(return_value={"tuNo": "1234567890"}),
    ) as mock_transport:
        result = await client.async_get_parcel("1234567890", "11000")
    assert result == {"tuNo": "1234567890"}
    mock_transport.assert_awaited_once_with(
        client._session,
        "gls-group.com",
        "CZ/en",
        "1234567890",
        "11000",
        country="CZ",
    )


async def test_it_dispatch_delegates_to_the_group_transport_with_its_own_country():
    """A non-CZ group-leaf country (IT) also dispatches through the same
    transport, passing its own country code through — GROUP_LEAF_COUNTRIES
    membership drives this, not a bare country == "CZ" check."""
    client = GlsApiClient(
        MagicMock(),
        "gls-group.com",
        "unused",
        country="IT",
        group_locale="IT/en",
    )
    with patch(
        "custom_components.gls.api.async_get_parcel_group",
        new=AsyncMock(return_value={"tuNo": "M4 663093258"}),
    ) as mock_transport:
        result = await client.async_get_parcel("M4663093258", "20121")
    assert result == {"tuNo": "M4 663093258"}
    mock_transport.assert_awaited_once_with(
        client._session,
        "gls-group.com",
        "IT/en",
        "M4663093258",
        "20121",
        country="IT",
    )
