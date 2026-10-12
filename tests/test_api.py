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


async def test_ca_dispatch_delegates_to_the_postcode_transport():
    client = GlsApiClient(MagicMock(), "web.gls-canada.com", "en-CA", country="CA")
    with patch(
        "custom_components.gls.api.async_get_parcel_ca",
        new=AsyncMock(return_value={"shipments": []}),
    ) as mock_transport:
        result = await client.async_get_parcel("CA12345678", "K1A0B1")
    assert result == {"shipments": []}
    mock_transport.assert_awaited_once_with(client._session, "CA12345678", "K1A0B1")


async def test_us_dispatch_delegates_to_the_us_transport():
    client = GlsApiClient(MagicMock(), "connect.gls-us.com", "en-US", country="US")
    with patch(
        "custom_components.gls.api.async_get_parcel_us",
        new=AsyncMock(return_value={"shipments": []}),
    ) as mock_transport:
        result = await client.async_get_parcel("12345678901234", "90210")
    assert result == {"shipments": []}
    mock_transport.assert_awaited_once_with(client._session, "12345678901234", "90210")


async def test_pl_dispatch_delegates_to_the_national_transport():
    client = GlsApiClient(MagicMock(), "mygls.gls-poland.com.pl", "pl-PL", country="PL")
    with patch(
        "custom_components.gls.api.async_get_parcel_pl",
        new=AsyncMock(return_value={"progressBarIdent": "DELIVERED"}),
    ) as mock_transport:
        result = await client.async_get_parcel("99900011122", "00-001")
    assert result == {"progressBarIdent": "DELIVERED"}
    mock_transport.assert_awaited_once_with(client._session, "99900011122", "00-001")


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


async def test_es_dispatch_delegates_to_the_signed_spanish_transport():
    client = GlsApiClient(MagicMock(), "api.consignee.gls-spain.es", "es-ES", country="ES")
    with patch(
        "custom_components.gls.api.async_get_parcel_es",
        new=AsyncMock(return_value={"expeditionCode": "990011223"}),
    ) as mock_transport:
        result = await client.async_get_parcel("990011223", "28001")
    assert result == {"expeditionCode": "990011223"}
    mock_transport.assert_awaited_once_with(client._session, "990011223", "28001")


async def test_pt_dispatch_uses_the_same_spanish_transport():
    """Portugal answers on the same host and the same request shape; it is a
    COUNTRIES row on one transport, not a transport of its own."""
    client = GlsApiClient(MagicMock(), "api.consignee.gls-spain.es", "pt-PT", country="PT")
    with patch(
        "custom_components.gls.api.async_get_parcel_es",
        new=AsyncMock(return_value={}),
    ) as mock_transport:
        await client.async_get_parcel("990011223", "1000-001")
    mock_transport.assert_awaited_once_with(client._session, "990011223", "1000-001")


async def test_a_numeric_french_code_skips_the_resolver_entirely():
    """Every French parcel that works today must stay on exactly the path it
    is on now, with no extra request."""
    client = GlsApiClient(
        MagicMock(), "gls-group.com", "unused", country="FR", group_locale="FR/en"
    )
    with (
        patch(
            "custom_components.gls.api.async_get_parcel_group",
            new=AsyncMock(return_value={"tuNo": "61234567890"}),
        ) as mock_transport,
        patch(
            "custom_components.gls.api.async_resolve_reference_fr",
            new=AsyncMock(return_value="SHOULD-NOT-BE-USED"),
        ) as mock_resolver,
    ):
        await client.async_get_parcel("61234567890", "39100")
    mock_resolver.assert_not_awaited()
    assert mock_transport.await_args.args[3] == "61234567890"


async def test_an_alphanumeric_french_reference_is_resolved_before_the_leaf():
    """The 8-character reference a GLS France notification carries is rejected
    by both group leaves, so it is translated into the numeric AWB first."""
    client = GlsApiClient(
        MagicMock(), "gls-group.com", "unused", country="FR", group_locale="FR/en"
    )
    with (
        patch(
            "custom_components.gls.api.async_get_parcel_group",
            new=AsyncMock(return_value={"tuNo": "61234567890"}),
        ) as mock_transport,
        patch(
            "custom_components.gls.api.async_resolve_reference_fr",
            new=AsyncMock(return_value="61234567890"),
        ) as mock_resolver,
    ):
        await client.async_get_parcel("AB12CD34", "39100")
    mock_resolver.assert_awaited_once_with(client._session, "AB12CD34")
    assert mock_transport.await_args.args[3] == "61234567890"


async def test_a_failed_french_resolution_falls_back_to_the_original_code():
    """This is what makes the resolver unable to regress anything: a failure
    tries the code on the leaf exactly as the integration did before."""
    client = GlsApiClient(
        MagicMock(), "gls-group.com", "unused", country="FR", group_locale="FR/en"
    )
    with (
        patch(
            "custom_components.gls.api.async_get_parcel_group",
            new=AsyncMock(return_value=None),
        ) as mock_transport,
        patch(
            "custom_components.gls.api.async_resolve_reference_fr",
            new=AsyncMock(return_value=None),
        ),
    ):
        await client.async_get_parcel("AB12CD34", "39100")
    assert mock_transport.await_args.args[3] == "AB12CD34"


async def test_another_group_country_never_touches_the_french_resolver():
    """The resolver is France-only: Italy's own codes are alphanumeric too,
    and they resolve on the leaf already."""
    client = GlsApiClient(
        MagicMock(), "gls-group.com", "unused", country="IT", group_locale="IT/en"
    )
    with (
        patch(
            "custom_components.gls.api.async_get_parcel_group",
            new=AsyncMock(return_value={}),
        ) as mock_transport,
        patch(
            "custom_components.gls.api.async_resolve_reference_fr",
            new=AsyncMock(return_value="SHOULD-NOT-BE-USED"),
        ) as mock_resolver,
    ):
        await client.async_get_parcel("M4663093258", "20121")
    mock_resolver.assert_not_awaited()
    assert mock_transport.await_args.args[3] == "M4663093258"
