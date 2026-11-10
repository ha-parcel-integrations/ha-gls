"""Danish GLS session tests with synthetic identities and token values."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.gls.countries.dk import session as session_module
from custom_components.gls.countries.dk.session import (
    GlsDkApiError,
    GlsDkAuthError,
    GlsDkSession,
    _required_string,
    _retry_after,
)

UID = "synthetic-user-identity"
EMAIL = "example@example.invalid"


def _ctx(status=200, body=None, *, raw=None, headers=None, chunks=None):
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    content = raw if raw is not None else json.dumps(body).encode()

    async def stream(size):
        for chunk in chunks if chunks is not None else [content]:
            yield chunk

    response.content.iter_chunked = stream
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _client(*responses):
    client = MagicMock()
    client.request = MagicMock(side_effect=list(responses))
    return client


def _login_body(**overrides):
    return {
        "localId": UID,
        "idToken": "first-id",
        "refreshToken": "refresh-one",
        "expiresIn": "3600",
        **overrides,
    }


def _refresh_body(**overrides):
    return {
        "user_id": UID,
        "id_token": "new-id",
        "refresh_token": "refresh-two",
        "expires_in": "3600",
        "token_type": "Bearer",
        **overrides,
    }


async def test_login_validates_profile_and_returns_only_persistable_state():
    updates = []
    client = _client(
        _ctx(body=_login_body()), _ctx(body={"identityId": UID, "email": EMAIL})
    )
    session = GlsDkSession(
        client, api_key="public-test-key", token_updated=updates.append
    )
    state = await session.async_login(EMAIL, "synthetic-password")
    assert state == {"uid": UID, "email": EMAIL, "refresh_token": "refresh-one"}
    assert updates == [state]
    assert await session.async_get_token() == "first-id"
    auth, profile = client.request.call_args_list
    assert auth.kwargs["json"]["returnSecureToken"] is True
    assert "Authorization" not in auth.kwargs["headers"]
    assert auth.kwargs["params"] == {"key": "public-test-key"}
    assert profile.args[1].endswith("/user/get/" + UID)
    assert profile.kwargs["headers"]["Authorization"] == "Bearer first-id"
    assert profile.kwargs["allow_redirects"] is False


async def test_login_profile_mismatch_does_not_publish_credentials():
    updates = []
    session = GlsDkSession(
        _client(
            _ctx(body=_login_body()), _ctx(body={"identityId": "other", "email": EMAIL})
        ),
        token_updated=updates.append,
    )
    with pytest.raises(GlsDkAuthError, match="profile identity"):
        await session.async_login(EMAIL, "synthetic-password")
    assert updates == []


async def test_restore_refreshes_once_for_concurrent_callers_and_persists_rotation():
    updates = []
    client = _client(_ctx(body=_refresh_body()))
    session = GlsDkSession(
        client, uid=UID, refresh_token="refresh-one", token_updated=updates.append
    )
    tokens = await asyncio.gather(*(session.async_get_token() for _ in range(6)))
    assert tokens == ["new-id"] * 6
    assert client.request.call_count == 1
    assert updates[0]["refresh_token"] == "refresh-two"
    assert client.request.call_args.kwargs["data"]["grant_type"] == "refresh_token"


async def test_refresh_same_credential_does_not_write_state_again():
    callback = MagicMock()
    session = GlsDkSession(
        _client(_ctx(body=_refresh_body(refresh_token="refresh-one"))),
        uid=UID,
        refresh_token="refresh-one",
        token_updated=callback,
    )
    assert await session.async_get_token() == "new-id"
    callback.assert_not_called()


async def test_expired_token_refreshes_and_missing_session_requires_signin():
    client = _client(_ctx(body=_refresh_body()))
    session = GlsDkSession(client, uid=UID, refresh_token="refresh-one")
    session._id_token = "expired"
    assert await session.async_get_token() == "new-id"
    with pytest.raises(GlsDkAuthError, match="sign-in"):
        await GlsDkSession(_client()).async_get_token()


@pytest.mark.parametrize("status", [401, 403])
async def test_feed_refreshes_rejected_token_once_and_uses_firebase_identity(status):
    feed = {"totalCount": 0, "items": []}
    client = _client(
        _ctx(body=_refresh_body(id_token="old-id")),
        _ctx(status),
        _ctx(body=_refresh_body(id_token="retry-id", refresh_token="refresh-three")),
        _ctx(body=feed),
    )
    session = GlsDkSession(client, uid=UID, refresh_token="refresh-one")
    assert await session.async_get_parcels() == feed
    calls = client.request.call_args_list
    assert len(calls) == 4
    assert calls[1].kwargs["params"] == {"userid": UID, "languagecode": "da"}
    assert calls[3].kwargs["headers"]["Authorization"] == "Bearer retry-id"


@pytest.mark.parametrize("status", [401, 403])
async def test_backend_rejection_surviving_a_refresh_is_not_an_auth_failure(status):
    client = _client(
        _ctx(body=_refresh_body()),
        _ctx(status),
        _ctx(body=_refresh_body()),
        _ctx(status),
    )
    with pytest.raises(GlsDkApiError) as error:
        await GlsDkSession(
            client, uid=UID, refresh_token="refresh-one"
        ).async_get_parcels()
    assert type(error.value) is GlsDkApiError
    assert error.value.status_code == status
    assert client.request.call_count == 4


async def test_refused_forced_refresh_requests_reauth():
    client = _client(
        _ctx(body=_refresh_body()),
        _ctx(401),
        _ctx(400, {"error": {"message": "TOKEN_EXPIRED"}}),
    )
    with pytest.raises(GlsDkAuthError):
        await GlsDkSession(
            client, uid=UID, refresh_token="refresh-one"
        ).async_get_parcels()
    assert client.request.call_count == 3


@pytest.mark.parametrize("status", [429, 500])
async def test_other_feed_failures_do_not_refresh_the_token(status):
    client = _client(
        _ctx(body=_refresh_body()), _ctx(status, headers={"Retry-After": "7"})
    )
    with pytest.raises(GlsDkApiError) as error:
        await GlsDkSession(
            client, uid=UID, refresh_token="refresh-one"
        ).async_get_parcels()
    assert type(error.value) is GlsDkApiError
    assert error.value.status_code == status
    assert error.value.retry_after == 7
    assert client.request.call_count == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_profile_rejection_during_login_is_not_a_credential_error(status):
    updates = []
    session = GlsDkSession(
        _client(_ctx(body=_login_body()), _ctx(status)), token_updated=updates.append
    )
    with pytest.raises(GlsDkApiError) as error:
        await session.async_login(EMAIL, "synthetic-password")
    assert type(error.value) is GlsDkApiError
    assert error.value.status_code == status
    assert updates == []


async def test_concurrent_rejection_reuses_an_already_refreshed_token():
    client = _client(_ctx(body=_refresh_body()))
    session = GlsDkSession(client, uid=UID, refresh_token="refresh-one")
    assert await session.async_get_token() == "new-id"
    assert await session.async_get_token(rejected_token="older-id") == "new-id"
    assert client.request.call_count == 1


@pytest.mark.parametrize(
    "code",
    [
        "INVALID_LOGIN_CREDENTIALS",
        "INVALID_PASSWORD",
        "EMAIL_NOT_FOUND",
        "INVALID_EMAIL",
        "USER_DISABLED",
        "INVALID_REFRESH_TOKEN",
        "TOKEN_EXPIRED",
        "USER_NOT_FOUND",
        "CREDENTIAL_TOO_OLD_LOGIN_AGAIN : details",
    ],
)
async def test_firebase_rejected_credentials_request_reauth(code):
    client = _client(_ctx(400, {"error": {"message": code}}))
    with pytest.raises(GlsDkAuthError) as error:
        await GlsDkSession(client).async_login(EMAIL, "synthetic-password")
    assert code not in str(error.value)
    assert error.value.status_code == 400


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, {"error": {"message": "TOO_MANY_ATTEMPTS_TRY_LATER"}}),
        (500, None),
        (302, None),
        (403, {"error": {"message": "API_KEY_INVALID"}}),
        (400, {"error": "unexpected"}),
    ],
)
async def test_other_http_failures_are_transient_and_do_not_disclose_body(status, body):
    client = _client(_ctx(status, body, headers={"Retry-After": "123"}))
    with pytest.raises(GlsDkApiError) as error:
        await GlsDkSession(client).async_login(EMAIL, "synthetic-password")
    assert type(error.value) is GlsDkApiError
    assert error.value.status_code == status
    assert error.value.retry_after == 123
    assert "synthetic-password" not in str(error.value)


@pytest.mark.parametrize(
    ("body", "raw"), [([], None), (None, b"not-json-private"), ({}, b"\xff")]
)
async def test_invalid_success_json_is_not_accepted(body, raw):
    client = _client(_ctx(body=body, raw=raw))
    with pytest.raises(GlsDkApiError, match="invalid JSON"):
        await GlsDkSession(client).async_login(EMAIL, "synthetic-password")


async def test_body_read_is_bounded(monkeypatch):
    monkeypatch.setattr(session_module, "_MAX_BODY_BYTES", 5)
    client = _client(_ctx(chunks=[b"123", b"456"]))
    with pytest.raises(GlsDkApiError, match="too large"):
        await GlsDkSession(client).async_login(EMAIL, "synthetic-password")


@pytest.mark.parametrize(
    "failure",
    [aiohttp.ClientError("private transport message"), TimeoutError("private timeout")],
)
async def test_transport_errors_are_redacted(failure):
    client = MagicMock()
    client.request.side_effect = failure
    with pytest.raises(GlsDkApiError, match="request failed") as error:
        await GlsDkSession(client).async_login(EMAIL, "synthetic-password")
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "value", [None, "", 123, "bad\nvalue", "bad\x7fvalue", "a" * 20001]
)
def test_response_string_validation(value):
    with pytest.raises(GlsDkApiError):
        _required_string({"test": value}, "test")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("0", None),
        ("-2", None),
        ("bad", None),
        ([], None),
        ("12", 12),
        ("999999", 86400),
    ],
)
def test_retry_after_validation(value, expected):
    assert _retry_after(value) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_in": "bad"},
        {"expires_in": None},
        {"expires_in": []},
        {"expires_in": 0},
        {"expires_in": True},
        {"expires_in": 999999999},
        {"user_id": "someone-else"},
        {"token_type": "Other"},
        {"id_token": ""},
    ],
)
async def test_bad_refresh_payload_leaves_existing_credentials_intact(overrides):
    session = GlsDkSession(
        _client(_ctx(body=_refresh_body(**overrides))),
        uid=UID,
        refresh_token="refresh-one",
    )
    with pytest.raises(GlsDkApiError):
        await session.async_get_token()
    assert session.refresh_token == "refresh-one"


async def test_missing_expiry_is_rejected():
    body = _refresh_body()
    del body["expires_in"]
    session = GlsDkSession(
        _client(_ctx(body=body)), uid=UID, refresh_token="refresh-one"
    )
    with pytest.raises(GlsDkApiError, match="lifetime"):
        await session.async_get_token()
