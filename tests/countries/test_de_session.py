"""Tests for the GLS Germany anonymous identity session (register/validate)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.gls.countries.de import session as session_module
from custom_components.gls.countries.de.session import (
    GlsDeSession,
    GlsDeSessionError,
)


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep the module's one-shot WARNING dedup state isolated per test.

    Production code deliberately warns only once per HA session (module-level
    state, same pattern as ``parcels.py``'s ``_unmapped_states_logged``) —
    reset it here so tests don't depend on run order.
    """
    session_module._token_lifetime_warned_hours.clear()
    session_module._recaptcha_warned = False
    yield
    session_module._token_lifetime_warned_hours.clear()
    session_module._recaptcha_warned = False


def _ctx(status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    """Build an async-context-manager mock standing in for ``session.post(...)``."""
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(
        return_value=json.dumps(body) if body is not None else ""
    )
    response.headers = headers or {}
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_with(*responses: MagicMock) -> MagicMock:
    """Build a session whose ``.post()`` returns ``responses`` in order."""
    session = MagicMock()
    session.post = MagicMock(side_effect=list(responses))
    return session


def _token_body(token: str = "tok", *, hours: float = 60) -> dict:
    """A register/validate success body with a lifetime of ``hours``."""
    now = datetime.now(timezone.utc)
    return {
        "accessToken": token,
        "issuedAt": int(now.timestamp()),
        "expiresAt": int((now + timedelta(hours=hours)).timestamp()),
    }


def _no_auth_header(call) -> bool:
    return "headers" not in call.kwargs or "Authorization" not in (
        call.kwargs.get("headers") or {}
    )


# ---------------------------------------------------------------------------
# register / get_token happy paths
# ---------------------------------------------------------------------------


async def test_register_mints_id_and_stores_token():
    session = _session_with(_ctx(201, _token_body("tok1")))
    de = GlsDeSession(session)
    app_id = await de.async_register()
    assert de.app_instance_id == app_id
    assert await de.async_get_token() == "tok1"
    assert session.post.call_count == 1
    call = session.post.call_args_list[0]
    assert call.args[0] == session_module.REGISTER_URL
    assert call.kwargs["json"] == {"appInstanceId": app_id}
    assert _no_auth_header(call)


async def test_get_token_reuses_cached_token_within_margin():
    session = _session_with(_ctx(201, _token_body("tok1", hours=60)))
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"
    assert await de.async_get_token() == "tok1"
    assert session.post.call_count == 1  # still fresh, no validate call


async def test_get_token_refreshes_when_inside_margin():
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=5)),  # already inside the 6h margin
        _ctx(200, _token_body("tok2", hours=60)),
    )
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok2"
    assert session.post.call_count == 2
    validate_call = session.post.call_args_list[1]
    assert validate_call.args[0] == session_module.VALIDATE_URL
    assert validate_call.kwargs["json"] == {"appInstanceId": de.app_instance_id}
    assert _no_auth_header(validate_call)


async def test_get_token_does_not_refresh_outside_margin():
    session = _session_with(_ctx(201, _token_body("tok1", hours=10)))
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"
    assert session.post.call_count == 1  # 10h > 6h margin, no refresh yet


async def test_existing_app_instance_id_still_refreshes_on_first_use():
    # A fresh process/instance always starts with no cached token, even when
    # `appInstanceId` was already persisted from a previous run.
    session = _session_with(_ctx(200, _token_body("tok1", hours=60)))
    de = GlsDeSession(session, app_instance_id="already-persisted-id")
    assert await de.async_get_token() == "tok1"
    call = session.post.call_args_list[0]
    assert call.args[0] == session_module.VALIDATE_URL
    assert call.kwargs["json"] == {"appInstanceId": "already-persisted-id"}


# ---------------------------------------------------------------------------
# 401-retry-once / 404-recovery
# ---------------------------------------------------------------------------


async def test_handle_unauthorized_forces_one_refresh():
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=60)),
        _ctx(200, _token_body("tok2", hours=60)),
    )
    de = GlsDeSession(session)
    await de.async_register()
    token = await de.async_handle_unauthorized()
    assert token == "tok2"
    assert session.post.call_count == 2


async def test_handle_unauthorized_does_not_loop_by_itself():
    # Each call to async_handle_unauthorized triggers exactly one validate;
    # not looping is the caller's contract (call it once per 401), which this
    # asserts by checking the call count matches the number of invocations.
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=60)),
        _ctx(200, _token_body("tok2", hours=60)),
    )
    de = GlsDeSession(session)
    await de.async_register()
    await de.async_handle_unauthorized()
    assert session.post.call_count == 2


async def test_validate_404_recovers_by_reregistering(caplog):
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=1)),  # register, near-expiry already
        _ctx(404),  # validate: the instance is gone
        _ctx(201, _token_body("tok2", hours=60)),  # recovery register
    )
    de = GlsDeSession(session)
    old_id = await de.async_register()
    with caplog.at_level("WARNING"):
        token = await de.async_get_token()
    assert token == "tok2"
    assert de.app_instance_id != old_id
    assert session.post.call_count == 3
    assert any("gone" in message.lower() for message in caplog.messages)
    assert any("recover" in message.lower() for message in caplog.messages)


async def test_pop_reregistered_reports_once_then_clears():
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=1)),
        _ctx(404),
        _ctx(201, _token_body("tok2", hours=60)),
    )
    de = GlsDeSession(session)
    await de.async_register()
    await de.async_get_token()
    assert de.pop_reregistered() is True
    assert de.pop_reregistered() is False


async def test_reregistered_stays_false_on_a_normal_refresh():
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=1)),
        _ctx(200, _token_body("tok2", hours=60)),
    )
    de = GlsDeSession(session)
    await de.async_register()
    await de.async_get_token()
    assert de.pop_reregistered() is False


# ---------------------------------------------------------------------------
# guard rails: no identity yet, unexpected status, network errors
# ---------------------------------------------------------------------------


async def test_get_token_before_register_raises():
    de = GlsDeSession(MagicMock())
    with pytest.raises(GlsDeSessionError):
        await de.async_get_token()


async def test_handle_unauthorized_before_register_raises():
    de = GlsDeSession(MagicMock())
    with pytest.raises(GlsDeSessionError):
        await de.async_handle_unauthorized()


async def test_register_raises_on_unexpected_status():
    session = _session_with(_ctx(500, {}))
    de = GlsDeSession(session)
    with pytest.raises(GlsDeSessionError):
        await de.async_register()


async def test_register_raises_when_response_has_no_access_token():
    session = _session_with(_ctx(201, {"issuedAt": 1, "expiresAt": 2}))
    de = GlsDeSession(session)
    with pytest.raises(GlsDeSessionError):
        await de.async_register()


async def test_register_propagates_network_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    de = GlsDeSession(session)
    with pytest.raises(aiohttp.ClientError):
        await de.async_register()


# ---------------------------------------------------------------------------
# §6 WARNING obligations: token lifetime anomaly, reCAPTCHA mentions
# ---------------------------------------------------------------------------


async def test_token_lifetime_anomaly_is_warned(caplog):
    session = _session_with(_ctx(201, _token_body("tok1", hours=2)))
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        await de.async_register()
    assert any("lifetime" in m.lower() for m in caplog.messages)


async def test_token_lifetime_anomaly_same_value_warns_once(caplog):
    session = _session_with(
        _ctx(201, _token_body("tok1", hours=2)),
        _ctx(200, _token_body("tok2", hours=2)),
    )
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        await de.async_register()
        # Force a second store_token() call with the same anomalous lifetime.
        de._expires_at = datetime.now(timezone.utc)
        await de.async_get_token()
    lifetime_warnings = [m for m in caplog.messages if "lifetime" in m.lower()]
    assert len(lifetime_warnings) == 1


async def test_normal_token_lifetime_is_not_warned(caplog):
    session = _session_with(_ctx(201, _token_body("tok1", hours=60)))
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        await de.async_register()
    assert not any("lifetime" in m.lower() for m in caplog.messages)


async def test_unparseable_timestamps_fall_back_without_crashing():
    session = _session_with(
        _ctx(201, {"accessToken": "tok1", "issuedAt": "??", "expiresAt": "??"})
    )
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"  # falls back to ~60h, still valid


async def test_missing_timestamp_fields_fall_back_without_crashing():
    session = _session_with(_ctx(201, {"accessToken": "tok1"}))
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"


async def test_boolean_timestamp_values_are_ignored():
    # issuedAt/expiresAt are Unix-epoch numbers on the wire; a stray bool
    # (int's subclass) must not be treated as one.
    session = _session_with(
        _ctx(201, {"accessToken": "tok1", "issuedAt": True, "expiresAt": False})
    )
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"


async def test_out_of_range_timestamp_falls_back_without_crashing():
    session = _session_with(
        _ctx(
            201,
            {"accessToken": "tok1", "issuedAt": 10**20, "expiresAt": 10**20 + 1},
        )
    )
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"


async def test_iso_timestamps_with_and_without_timezone_are_parsed():
    now = datetime.now(timezone.utc)
    issued = now - timedelta(hours=2)
    expires = now + timedelta(hours=60)
    session = _session_with(
        _ctx(
            201,
            {
                "accessToken": "tok1",
                # naive (no offset) — must be treated as UTC
                "issuedAt": issued.strftime("%Y-%m-%dT%H:%M:%S"),
                # explicit Zulu suffix
                "expiresAt": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    )
    de = GlsDeSession(session)
    await de.async_register()
    assert await de.async_get_token() == "tok1"


async def test_recaptcha_in_body_is_warned(caplog):
    session = _session_with(
        _ctx(403, {"displayableMessage": "please solve the recaptcha"})
    )
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsDeSessionError):
            await de.async_register()
    assert any("recaptcha" in m.lower() for m in caplog.messages)


async def test_recaptcha_header_alone_is_warned(caplog):
    session = _session_with(
        _ctx(201, _token_body("tok1"), headers={"x-recaptcha-token": "1"})
    )
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        await de.async_register()
    assert any("recaptcha" in m.lower() for m in caplog.messages)


async def test_recaptcha_warned_only_once(caplog):
    session = _session_with(
        _ctx(403, {"displayableMessage": "recaptcha required"}),
        _ctx(403, {"displayableMessage": "recaptcha required"}),
    )
    de = GlsDeSession(session)
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsDeSessionError):
            await de.async_register()
        with pytest.raises(GlsDeSessionError):
            await de.async_register()
    recaptcha_warnings = [m for m in caplog.messages if "recaptcha" in m.lower()]
    assert len(recaptcha_warnings) == 1
