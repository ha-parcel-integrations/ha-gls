"""Polish GLS session tests with synthetic identities and token values."""

from __future__ import annotations

import base64
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.gls.account.countries.pl import session as session_module
from custom_components.gls.account.countries.pl.session import (
    DETAILS_KEY,
    GlsPlApiError,
    GlsPlAuthError,
    GlsPlSession,
)

# Captured before the autouse fixture below replaces it.
_REAL_TOKEN_CLIENT = GlsPlSession._token_client


@pytest.fixture(autouse=True)
def _token_on_shared_mock():
    """Route token calls through the mocked shared client.

    So every test here sees the token endpoint on ``client.post``, not the
    real cookieless client. A test about how token calls go out (cookies,
    connector) must restore ``_REAL_TOKEN_CLIENT`` itself, as the
    ``*_carries_no_shared_cookies`` tests do.
    """

    @contextlib.asynccontextmanager
    async def shared(self):
        yield self._session

    with patch.object(GlsPlSession, "_token_client", shared):
        yield


UID = "c3b2d5ba-5c1f-4184-af2e-synthetic"
PHONE = "600000000"


def _jwt(sub=UID, name=f"+48{PHONE}"):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub, "name": name}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"hdr.{payload}.sig"


def _async_cm(value):
    """An ``async with`` context manager that yields ``value``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _api_ctx(status=200, body=None):
    """A JSON response read through ``content.iter_chunked``."""
    response = MagicMock()
    response.status = status
    response.history = ()
    response.headers = MagicMock()
    content = b"" if body is None else json.dumps(body).encode()

    async def stream(size):
        yield content

    response.content.iter_chunked = stream
    return _async_cm(response)


class _Headers:
    def __init__(self, location=None):
        self._location = location

    def get(self, key, default=None):
        return self._location if key == "Location" else default

    def getall(self, key, default=None):
        return default if default is not None else []


def _text_ctx(text="", location=None):
    response = MagicMock()
    response.status = 200
    response.history = ()
    response.headers = _Headers(location)
    response.text = AsyncMock(return_value=text)
    return _async_cm(response)


def _token_body(access=None, refresh="refresh-two", expires_in=3600):
    return {
        "access_token": access or _jwt(),
        "refresh_token": refresh,
        "expires_in": expires_in,
    }


async def test_token_refresh_rotates_and_persists():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body(refresh="r2"))])
    saved = []
    session = GlsPlSession(
        client, uid=UID, refresh_token="r1", token_updated=saved.append
    )
    token = await session.async_get_token()
    assert token == _jwt()
    assert session.uid == UID
    assert session.refresh_token == "r2"
    assert saved and saved[-1]["refresh_token"] == "r2"


async def test_token_cached_until_expiry():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body())])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    first = await session.async_get_token()
    second = await session.async_get_token()
    assert first == second
    assert client.post.call_count == 1  # no second refresh


async def test_missing_refresh_token_requires_signin():
    session = GlsPlSession(MagicMock(), uid=UID)
    with pytest.raises(GlsPlAuthError):
        await session.async_get_token()


async def test_invalid_grant_is_auth_error():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(status=400, body={"error": "invalid_grant"})]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlAuthError):
        await session.async_get_token()


async def test_backend_5xx_on_token_is_retryable():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(status=503, body={"error": "x"})])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError) as err:
        await session.async_get_token()
    assert not isinstance(err.value, GlsPlAuthError)


async def test_unreadable_200_on_token_is_retryable_not_a_refusal():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(status=200, body=None)])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError) as err:
        await session.async_get_token()
    assert not isinstance(err.value, GlsPlAuthError)
    assert str(err.value) == "GLS Poland token response was not readable"
    assert err.value.status_code is None
    assert session.refresh_token == "r1"


def _cookieless_client_cm(response_body):
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=response_body)])
    return _async_cm(client)


def _assert_cookieless(factory, shared):
    kwargs = factory.call_args.kwargs
    assert isinstance(kwargs["cookie_jar"], aiohttp.DummyCookieJar)
    assert kwargs["connector"] is shared.connector
    assert kwargs["connector_owner"] is False
    shared.post.assert_not_called()


async def test_token_refresh_carries_no_shared_cookies():
    """B2C's SSO cookie in HA's shared jar broke every refresh until restart."""
    shared = MagicMock()
    session = GlsPlSession(shared, uid=UID, refresh_token="r1")
    with (
        patch.object(GlsPlSession, "_token_client", _REAL_TOKEN_CLIENT),
        patch.object(
            session_module.aiohttp,
            "ClientSession",
            return_value=_cookieless_client_cm(_token_body()),
        ) as factory,
    ):
        assert await session.async_get_token() == _jwt()
    _assert_cookieless(factory, shared)


async def test_code_exchange_carries_no_shared_cookies():
    """Sign-in's code exchange goes through the same cookieless client."""
    shared = MagicMock()
    session = GlsPlSession(shared)
    with (
        patch.object(GlsPlSession, "_token_client", _REAL_TOKEN_CLIENT),
        patch.object(
            GlsPlSession,
            "_b2c_authorize",
            AsyncMock(return_value=("code", "verifier")),
        ),
        patch.object(
            session_module.aiohttp,
            "ClientSession",
            side_effect=[
                _async_cm(MagicMock()),  # the sign-in's browser client
                _cookieless_client_cm(_token_body()),
            ],
        ) as factory,
    ):
        await session.async_login(PHONE, "secret")
    _assert_cookieless(factory, shared)
    assert session.uid == UID


async def test_identity_change_is_auth_error():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body(access=_jwt(sub="someone-else")))]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlAuthError):
        await session.async_get_token()


async def test_get_parcels_registers_session_then_pages_and_enriches():
    item = {"trackingUid": "u-1", "shipmentNo": "490000000001"}
    feed = {"content": [item], "last": True, "totalElements": 1}
    details = {"trackingShipment": {"weight": 1.2}}
    client = MagicMock()
    # post: token refresh, then the session registration
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    # request: feed page, then the details fetch
    client.request = MagicMock(
        side_effect=[_api_ctx(body=feed), _api_ctx(body=details)]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    result = await session.async_get_parcels()
    assert result["totalElements"] == 1
    assert result["items"][0][DETAILS_KEY] == details
    # The registration carried a client-minted sessionId, and no api key/signature.
    reg_call = client.post.call_args_list[-1]
    assert reg_call.kwargs["json"]["sessionId"]
    assert reg_call.kwargs["json"]["deviceType"] == "ANDROID"
    assert "X-Api-Key" not in reg_call.kwargs["headers"]
    assert "X-Signature" not in reg_call.kwargs["headers"]


async def test_feed_retries_once_after_rejected_token():
    feed = {"content": [], "last": True, "totalElements": 0}
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[
            _api_ctx(body=_token_body(refresh="r2")),  # first token
            _api_ctx(status=200),  # first register
            _api_ctx(body=_token_body(refresh="r3")),  # forced refresh
            _api_ctx(status=200),  # re-register
        ]
    )
    client.request = MagicMock(side_effect=[_api_ctx(status=401), _api_ctx(body=feed)])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    result = await session.async_get_parcels()
    assert result["totalElements"] == 0
    assert client.request.call_count == 2  # retried after the 401


async def test_details_failure_degrades_to_feed_row():
    item = {"trackingUid": "u-1", "shipmentNo": "490000000001"}
    feed = {"content": [item], "last": True, "totalElements": 1}
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    # feed ok, details 500 twice (initial + retry) -> degrade to None
    client.request = MagicMock(
        side_effect=[
            _api_ctx(body=feed),
            _api_ctx(status=500),
            _api_ctx(status=500),
        ]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    result = await session.async_get_parcels()
    assert result["items"][0][DETAILS_KEY] is None


async def test_full_b2c_login_flow():
    settings_html = (
        '<html><script>var SETTINGS = {"csrf":"CSRF","transId":'
        '"StateProperties=eyJ4IjoxfQ"};</script></html>'
    )
    browser = MagicMock()
    browser.get = MagicMock(
        side_effect=[
            _text_ctx(text=settings_html),  # authorize page
            _text_ctx(location="msauth://pl.gls.mygls/x?code=AUTH_CODE&state=s"),
        ]
    )
    browser.post = MagicMock(side_effect=[_text_ctx(text='{"status":"200"}')])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body(refresh="r1"))])
    session = GlsPlSession(client)
    with patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm):
        state = await session.async_login(PHONE, "secret-password")
    assert state["uid"] == UID
    assert state["phone"] == PHONE
    assert state["refresh_token"] == "r1"
    # The authorization code reached the token exchange.
    assert client.post.call_args.kwargs["data"]["code"] == "AUTH_CODE"


async def test_login_rejects_bad_credentials():
    settings_html = (
        '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    )
    browser = MagicMock()
    browser.get = MagicMock(side_effect=[_text_ctx(text=settings_html)])
    browser.post = MagicMock(
        side_effect=[_text_ctx(text='{"status":"400","errorCode":"x"}')]
    )
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlAuthError),
    ):
        await session.async_login(PHONE, "wrong")


async def test_login_unparseable_page_is_retryable_not_auth():
    """Changed B2C markup is not the user's wrong password."""
    browser = MagicMock()
    browser.get = MagicMock(side_effect=[_text_ctx(text="<html>no settings</html>")])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError) as err,
    ):
        await session.async_login(PHONE, "x")
    assert not isinstance(err.value, GlsPlAuthError)


def _err_ctx(exc):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=exc)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_token_network_error_is_retryable():

    client = MagicMock()
    client.post = MagicMock(side_effect=[_err_ctx(aiohttp.ClientError("boom"))])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError) as err:
        await session.async_get_token()
    assert not isinstance(err.value, GlsPlAuthError)


async def test_token_without_access_token_errors():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body={"refresh_token": "r2"})])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_token()


async def test_token_without_identity_claim_errors():
    bad = "hdr." + base64.urlsafe_b64encode(b'{"x":1}').rstrip(b"=").decode() + ".sig"
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body(access=bad))])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_token()


async def test_response_too_large_is_rejected():
    big = {"a": "x" * (5 * 1024 * 1024)}
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=big)])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_token()


async def test_session_registration_rejected_bubbles_up():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=500)]
    )
    client.request = MagicMock(side_effect=[])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()


async def test_feed_not_a_dict_is_rejected():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    client.request = MagicMock(side_effect=[_api_ctx(body=["nope"])])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()


async def test_feed_content_not_list_is_rejected():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    client.request = MagicMock(side_effect=[_api_ctx(body={"content": "x"})])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()


async def test_api_non_reject_error_is_not_retried():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    client.request = MagicMock(side_effect=[_api_ctx(status=500)])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()
    assert client.request.call_count == 1  # 500 is not retried


async def test_browser_network_error_during_login():

    browser = MagicMock()
    browser.get = MagicMock(side_effect=[_err_ctx(aiohttp.ClientError("down"))])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError),
    ):
        await session.async_login(PHONE, "x")


class _CookieHeaders(_Headers):
    def __init__(self, location=None, set_cookie=None):
        super().__init__(location)
        self._set_cookie = set_cookie or []

    def getall(self, key, default=None):
        if key == "Set-Cookie":
            return self._set_cookie
        return default if default is not None else []


def _text_ctx_cookie(text="", location=None, set_cookie=None):
    response = MagicMock()
    response.status = 200
    response.history = ()
    response.headers = _CookieHeaders(location, set_cookie)
    response.text = AsyncMock(return_value=text)
    return _async_cm(response)


async def test_jwt_with_corrupt_payload_errors():
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body(access="a.!!!.c"))])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_token()


async def test_token_bad_expires_defaults_without_error():
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body={"access_token": _jwt(), "expires_in": "soon"})]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    assert await session.async_get_token() == _jwt()


async def test_api_request_client_error_bubbles():

    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    client.request = MagicMock(side_effect=[_err_ctx(aiohttp.ClientError("net"))])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()


async def test_register_client_error_bubbles():

    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _err_ctx(aiohttp.ClientError("x"))]
    )
    client.request = MagicMock(side_effect=[])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError):
        await session.async_get_parcels()


async def test_login_with_cookies_and_selfasserted_non_json():
    html = '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    browser = MagicMock()
    browser.get = MagicMock(
        side_effect=[
            _text_ctx_cookie(text=html, set_cookie=["x-ms-cpim-csrf|a=v; path=/"])
        ]
    )
    browser.post = MagicMock(side_effect=[_text_ctx_cookie(text="not json")])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError) as err,
    ):
        await session.async_login(PHONE, "x")
    assert not isinstance(err.value, GlsPlAuthError)
    # The anti-forgery cookie was carried into the SelfAsserted POST.
    assert "Cookie" in browser.post.call_args.kwargs["headers"]


async def test_login_confirmed_without_code_errors():
    html = '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    browser = MagicMock()
    browser.get = MagicMock(
        side_effect=[
            _text_ctx(text=html),
            _text_ctx(location="msauth://pl.gls.mygls/x?error=access_denied"),
        ]
    )
    browser.post = MagicMock(side_effect=[_text_ctx(text='{"status":"200"}')])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError) as err,
    ):
        await session.async_login(PHONE, "x")
    # The credentials were accepted; a flow fault must not send the user to reauth.
    assert not isinstance(err.value, GlsPlAuthError)


@pytest.mark.parametrize(
    "verdict", ['{"status":"429"}', '{"status":"500"}', "{}", '["200"]']
)
async def test_selfasserted_non_credential_answer_is_retryable(verdict):
    """Only B2C's "400" credential refusal means invalid_auth."""
    html = '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    browser = MagicMock()
    browser.get = MagicMock(side_effect=[_text_ctx(text=html)])
    browser.post = MagicMock(side_effect=[_text_ctx(text=verdict)])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    session = GlsPlSession(MagicMock())
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError) as err,
    ):
        await session.async_login(PHONE, "x")
    assert not isinstance(err.value, GlsPlAuthError)


async def test_details_auth_failure_propagates_to_reauth():
    """A refresh refused mid-poll is reauth, not "no details"."""
    item = {"trackingUid": "u-1", "shipmentNo": "490000000001"}
    feed = {"content": [item], "last": True, "totalElements": 1}
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[
            _api_ctx(body=_token_body()),  # token
            _api_ctx(status=200),  # register
            _api_ctx(status=400, body={"error": "invalid_grant"}),  # forced refresh
        ]
    )
    client.request = MagicMock(side_effect=[_api_ctx(body=feed), _api_ctx(status=401)])
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlAuthError):
        await session.async_get_parcels()


async def test_details_failure_reuses_last_good_details_then_forgets_departed():
    item = {"trackingUid": "u-1", "shipmentNo": "490000000001"}
    feed = {"content": [item], "last": True, "totalElements": 1}
    empty = {"content": [], "last": True, "totalElements": 0}
    details = {"trackingShipment": {"weight": 1.2}}
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(body=_token_body()), _api_ctx(status=200)]
    )
    client.request = MagicMock(
        side_effect=[
            _api_ctx(body=dict(feed, content=[dict(item)])),
            _api_ctx(body=details),  # poll 1: details ok
            _api_ctx(body=dict(feed, content=[dict(item)])),
            _api_ctx(status=500),  # poll 2: details fail (a 500 is not retried)
            _api_ctx(body=dict(feed, content=[dict(item)])),
            _api_ctx(body=["not", "a", "dict"]),  # poll 3: details malformed
            _api_ctx(body=empty),  # poll 4: parcel gone
            _api_ctx(body=dict(feed, content=[dict(item)])),
            _api_ctx(status=500),  # poll 5: back, but nothing cached any more
        ]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    assert (await session.async_get_parcels())["items"][0][DETAILS_KEY] == details
    assert (await session.async_get_parcels())["items"][0][DETAILS_KEY] == details
    assert (await session.async_get_parcels())["items"][0][DETAILS_KEY] == details
    assert (await session.async_get_parcels())["items"] == []
    assert (await session.async_get_parcels())["items"][0][DETAILS_KEY] is None


async def test_details_rejection_surviving_the_refresh_fails_the_poll_once():
    """A lasting 401 is UpdateFailed material: one forced refresh, not one per parcel."""
    items = [
        {"trackingUid": "u-1", "shipmentNo": "490000000001"},
        {"trackingUid": "u-2", "shipmentNo": "490000000002"},
    ]
    feed = {"content": items, "last": True, "totalElements": 2}
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[
            _api_ctx(body=_token_body(refresh="r2")),  # token
            _api_ctx(status=200),  # register
            _api_ctx(body=_token_body(refresh="r3")),  # the one forced refresh
            _api_ctx(status=200),  # re-register
        ]
    )
    client.request = MagicMock(
        side_effect=[_api_ctx(body=feed), _api_ctx(status=401), _api_ctx(status=401)]
    )
    session = GlsPlSession(client, uid=UID, refresh_token="r1")
    with pytest.raises(GlsPlApiError) as err:
        await session.async_get_parcels()
    assert not isinstance(err.value, GlsPlAuthError)
    assert err.value.status_code == 401
    assert client.post.call_count == 4  # no second refresh for the second parcel
    assert client.request.call_count == 3


async def test_browser_steps_send_no_browser_user_agent():
    html = '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    browser = MagicMock()
    browser.get = MagicMock(
        side_effect=[
            _text_ctx(text=html),
            _text_ctx(location="msauth://pl.gls.mygls/x?code=AUTH_CODE"),
        ]
    )
    browser.post = MagicMock(side_effect=[_text_ctx(text='{"status":"200"}')])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.post = MagicMock(side_effect=[_api_ctx(body=_token_body())])
    session = GlsPlSession(client)
    with patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm):
        await session.async_login(PHONE, "x")
    calls = [*browser.get.call_args_list, *browser.post.call_args_list]
    agents = {call.kwargs["headers"]["User-Agent"] for call in calls}
    assert agents == {session_module._APP_UA}
    assert not any("Mozilla" in agent for agent in agents)


async def test_refused_code_exchange_after_accepted_credentials_is_retryable():
    """SelfAsserted said yes; a refused code exchange is a flow fault, not invalid_auth."""
    html = '<script>SETTINGS = {"csrf":"C","transId":"StateProperties=x"}</script>'
    browser = MagicMock()
    browser.get = MagicMock(
        side_effect=[
            _text_ctx(text=html),
            _text_ctx(location="msauth://pl.gls.mygls/x?code=AUTH_CODE"),
        ]
    )
    browser.post = MagicMock(side_effect=[_text_ctx(text='{"status":"200"}')])
    browser_cm = MagicMock()
    browser_cm.__aenter__ = AsyncMock(return_value=browser)
    browser_cm.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.post = MagicMock(
        side_effect=[_api_ctx(status=400, body={"error": "invalid_grant"})]
    )
    session = GlsPlSession(client)
    with (
        patch.object(session_module.aiohttp, "ClientSession", return_value=browser_cm),
        pytest.raises(GlsPlApiError) as err,
    ):
        await session.async_login(PHONE, "x")
    assert not isinstance(err.value, GlsPlAuthError)
    assert session.phone is None and session.refresh_token is None
