"""Tests for the GLS Italy national history leaf and its use by the group leaf.

Italy keeps its status from the group leaf; this leaf only supplies history.
The tests pin both halves of that: the leaf's own behaviour, and the rule that
it never replaces a history the group leaf already produced.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.gls.const import ParcelStatus
from custom_components.gls.countries import it
from custom_components.gls.countries.group import (
    async_get_parcel_group,
    normalize_parcel_group,
)
from custom_components.gls.countries.it import async_get_history_it

HOST = "gls-group.com"
AWB = "662377471"


@pytest.fixture(autouse=True)
def _clear_failure_log():
    it._failures_logged.clear()
    yield
    it._failures_logged.clear()


def _national_row(description: str, date: str, time: str) -> dict:
    return {
        "date": date,
        "time": time,
        "evtDscr": description,
        "address": {"city": "MILANO"},
        "forward": False,
    }


def _national_reply() -> dict:
    """A structural sample of an rstt030 reply. No event codes exist here."""
    return {
        "tuStatus": [
            {
                "tuNo": "UF 662377471",
                "referenceNo": "UF 662377471",
                "postalCode": "",
                "infos": [{"type": "WEIGHT", "value": "   0,8 Kg"}],
                "history": [
                    _national_row("Delivered.", "2026-10-09", "09:12:00"),
                    _national_row(
                        "Shipment entrusted to area correspondent, delivery is "
                        "expected within the next three business days.",
                        "2026-10-07",
                        "18:40:00",
                    ),
                ],
            }
        ]
    }


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


# ---------------------------------------------------------------------------
# The leaf itself
# ---------------------------------------------------------------------------


async def test_the_national_history_rows_come_back_newest_first():
    rows = await async_get_history_it(_session(200, _national_reply()), HOST, AWB)
    assert [row["evtDscr"] for row in rows] == [
        "Delivered.",
        "Shipment entrusted to area correspondent, delivery is expected within "
        "the next three business days.",
    ]


async def test_type_nat_is_pinned_unconditionally():
    """Omitting it answers 400 with an HTML body — exactly the shape the
    suite's rule says must never be read as a missing parcel, and here
    produced by our own malformed request."""
    session = _session(200, _national_reply())
    await async_get_history_it(session, HOST, AWB)
    url = session.get.call_args.args[0]
    assert "type=NAT" in url
    assert "/IT/en/rstt030" in url


async def test_the_number_is_sent_unspaced():
    """The service prints the number with a space after the depot code and
    rejects that form on input."""
    session = _session(200, _national_reply())
    await async_get_history_it(session, HOST, "UF 662377471")
    assert "match=UF662377471" in session.get.call_args.args[0]


async def test_an_unknown_number_yields_no_history():
    body = {"exceptionText": "Unfortunately there are no results."}
    assert await async_get_history_it(_session(404, body), HOST, AWB) is None


async def test_an_html_body_yields_no_history():
    session = MagicMock()
    response = AsyncMock()
    response.status = 400
    response.json = AsyncMock(side_effect=aiohttp.ContentTypeError(MagicMock(), ()))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)
    assert await async_get_history_it(session, HOST, AWB) is None


async def test_a_network_error_yields_no_history_rather_than_raising():
    """This call is additive, so a failure must degrade rather than drop the
    parcel the group leaf already answered for."""
    session = _session(200, None, raises=aiohttp.ClientError())
    assert await async_get_history_it(session, HOST, AWB) is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tuStatus": []},
        {"tuStatus": ["not a dict"]},
        {"tuStatus": [{"history": []}]},
        {"tuStatus": [{}]},
    ],
)
async def test_a_reply_without_usable_history_yields_none(body):
    assert await async_get_history_it(_session(200, body), HOST, AWB) is None


@pytest.mark.parametrize("awb", ["", "   "])
async def test_an_empty_number_is_never_sent(awb):
    session = _session(200, _national_reply())
    assert await async_get_history_it(session, HOST, awb) is None
    session.get.assert_not_called()


async def test_a_failure_is_logged_once_at_debug(caplog):
    with caplog.at_level(logging.DEBUG):
        await async_get_history_it(_session(500, {}), HOST, AWB)
        await async_get_history_it(_session(500, {}), HOST, AWB)
    assert sum("national history is unavailable" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# How the group leaf uses it
# ---------------------------------------------------------------------------


def _group_body(history: list | None) -> dict:
    body = {
        "tuNo": "UF 662377471",
        "progressBar": {"statusInfo": "DELIVERED", "statusText": "Delivered"},
    }
    if history is not None:
        body["history"] = history
    return body


def _group_session(body: dict) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.text = AsyncMock(return_value=__import__("json").dumps(body))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)
    return session


async def test_an_italian_body_with_no_history_is_filled_from_the_national_leaf():
    session = _group_session(_group_body(None))
    with patch(
        "custom_components.gls.countries.group.async_get_history_it",
        new=AsyncMock(return_value=_national_reply()["tuStatus"][0]["history"]),
    ) as mock_leaf:
        body = await async_get_parcel_group(
            session, HOST, "IT/en", AWB, "20121", country="IT"
        )
    mock_leaf.assert_awaited_once_with(session, HOST, AWB)
    assert [row["evtDscr"] for row in body["history"]] == [
        "Delivered.",
        "Shipment entrusted to area correspondent, delivery is expected within "
        "the next three business days.",
    ]


async def test_a_history_the_group_leaf_already_returned_is_never_replaced():
    """An rstt028 history carries per-event evtNo codes the national rows do
    not, so the group's own answer is strictly richer and is kept."""
    own = [{"date": "2026-10-09", "time": "09:12:00", "evtDscr": "Delivered", "evtNo": "3.0"}]
    session = _group_session(_group_body(own))
    with patch(
        "custom_components.gls.countries.group.async_get_history_it",
        new=AsyncMock(return_value=_national_reply()["tuStatus"][0]["history"]),
    ) as mock_leaf:
        body = await async_get_parcel_group(
            session, HOST, "IT/en", AWB, "20121", country="IT"
        )
    mock_leaf.assert_not_awaited()
    assert body["history"] == own


async def test_no_other_group_country_consults_the_italian_leaf():
    """rstt030 is published for IT alone — eleven other country paths 403."""
    session = _group_session(_group_body(None))
    with patch(
        "custom_components.gls.countries.group.async_get_history_it",
        new=AsyncMock(return_value=[]),
    ) as mock_leaf:
        await async_get_parcel_group(
            session, HOST, "CZ/en", AWB, "11000", country="CZ"
        )
    mock_leaf.assert_not_awaited()


async def test_a_national_leaf_failure_leaves_the_parcel_otherwise_intact():
    session = _group_session(_group_body(None))
    with patch(
        "custom_components.gls.countries.group.async_get_history_it",
        new=AsyncMock(return_value=None),
    ):
        body = await async_get_parcel_group(
            session, HOST, "IT/en", AWB, "20121", country="IT"
        )
    assert body["progressBar"]["statusInfo"] == "DELIVERED"
    assert "history" not in body


def test_italy_keeps_its_status_from_the_group_leaf_and_its_events_stay_neutral():
    """The national rows have no event code of any kind, so each event's own
    status is None — a supported, neutral state — while the parcel's status
    still comes from the group's machine vocabulary."""
    body = _group_body(_national_reply()["tuStatus"][0]["history"])
    parcel = normalize_parcel_group(
        body, postal_code="20121", country="IT", include_history=True, parcel_no=AWB
    )
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert [event["status"] for event in parcel["history"]] == [None, None]
    assert parcel["history"][-1]["raw_status"] == "Delivered."
    # Oldest first, from a newest-first wire order.
    assert parcel["history"][0]["timestamp"] < parcel["history"][-1]["timestamp"]
