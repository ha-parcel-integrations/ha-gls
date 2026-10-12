"""Tests for the GLS France reference resolver.

The resolver's whole value is that it cannot regress anything: a plain numeric
AWB must never reach it, and every failure path must fall back to the code the
user typed. Most of this file pins exactly that.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.gls.countries import fr
from custom_components.gls.countries.fr import (
    async_resolve_reference_fr,
    needs_resolving,
)

# Synthetic: the 8-character form a GLS France notification carries, and the
# numeric AWB the group index holds.
REFERENCE = "AB12CD34"
AWB = "61234567890"


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    fr._resolved.clear()
    fr._failures_logged.clear()
    yield
    fr._resolved.clear()
    fr._failures_logged.clear()


def _session(status: int, body: object, *, raises: Exception | None = None) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(side_effect=raises) if raises else MagicMock(
        return_value=context
    )
    return session


def _reply() -> dict:
    return {
        "colis": {
            "trackid": REFERENCE,
            "numeroGp": AWB,
            "numeroalphaColis": "1234567",
            "statutColis": "LIV",
        },
        "evenements": [],
    }


# ---------------------------------------------------------------------------
# needs_resolving — the guard that keeps today's parcels on today's path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["61234567890", "123", "  61234567890  "])
def test_a_plain_numeric_awb_never_needs_resolving(code):
    """The group index holds this form already, so it must pass through
    untouched — no extra request for any French parcel that works today."""
    assert needs_resolving(code) is False


@pytest.mark.parametrize("code", [REFERENCE, "ab12cd34", "A1B2C3D4"])
def test_an_alphanumeric_reference_needs_resolving(code):
    assert needs_resolving(code) is True


@pytest.mark.parametrize("code", ["", "   "])
def test_an_empty_code_needs_nothing(code):
    assert needs_resolving(code) is False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def test_a_reference_resolves_to_the_numeric_awb():
    session = _session(200, _reply())
    assert await async_resolve_reference_fr(session, REFERENCE) == AWB


async def test_the_numeric_awb_is_the_only_form_accepted():
    """Three identifier forms come back in one reply. Picking the wrong one
    hands the group leaf a code it cannot resolve, which looks exactly like a
    carrier outage."""
    reply = _reply()
    reply["colis"]["numeroGp"] = None
    assert await async_resolve_reference_fr(_session(200, reply), REFERENCE) is None


async def test_a_non_numeric_numerogp_is_refused():
    reply = _reply()
    reply["colis"]["numeroGp"] = "NOT-A-NUMBER"
    assert await async_resolve_reference_fr(_session(200, reply), REFERENCE) is None


async def test_a_resolution_is_cached_for_the_process():
    session = _session(200, _reply())
    assert await async_resolve_reference_fr(session, REFERENCE) == AWB
    assert await async_resolve_reference_fr(session, REFERENCE) == AWB
    # The mapping is a property of the shipment and does not change, so the
    # second refresh must not cost another request.
    assert session.get.call_count == 1


async def test_whitespace_around_the_reference_does_not_defeat_the_cache():
    session = _session(200, _reply())
    await async_resolve_reference_fr(session, REFERENCE)
    await async_resolve_reference_fr(session, f"  {REFERENCE}  ")
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Every failure falls back, and never fails the poll
# ---------------------------------------------------------------------------


async def test_an_unknown_reference_resolves_to_none():
    """The 404 names the code it was asked for, which is this suite's
    strongest semantic not-found."""
    assert await async_resolve_reference_fr(_session(404, None), REFERENCE) is None


async def test_a_transport_failure_resolves_to_none_rather_than_raising():
    assert await async_resolve_reference_fr(_session(500, None), REFERENCE) is None


async def test_a_network_error_resolves_to_none_rather_than_raising():
    """An optional translation step in front of the real transport must
    degrade to the old behaviour, not drop the parcel."""
    session = _session(200, None, raises=aiohttp.ClientError())
    assert await async_resolve_reference_fr(session, REFERENCE) is None


async def test_a_non_json_body_resolves_to_none():
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(side_effect=aiohttp.ContentTypeError(MagicMock(), ()))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)
    assert await async_resolve_reference_fr(session, REFERENCE) is None


async def test_a_reply_without_colis_resolves_to_none():
    assert await async_resolve_reference_fr(_session(200, {}), REFERENCE) is None


async def test_a_failure_is_not_cached_so_a_later_refresh_retries():
    session = _session(500, None)
    await async_resolve_reference_fr(session, REFERENCE)
    await async_resolve_reference_fr(session, REFERENCE)
    assert session.get.call_count == 2


async def test_a_failure_never_logs_the_reference(caplog):
    """The reference is the user's own parcel identifier."""
    with caplog.at_level(logging.DEBUG):
        await async_resolve_reference_fr(_session(500, None), REFERENCE)
    assert all(REFERENCE not in record.message for record in caplog.records)
