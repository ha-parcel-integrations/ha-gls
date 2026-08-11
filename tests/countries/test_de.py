"""Tests for GLS Germany transport, normalize_parcel_de and map_parcel_status_de.

Fixtures are built from the captured delivered-parcel body in germany.md
(redacted AWB/UUID, ``latestStatusText: ""``, no status field at all) — the
only wire evidence that exists for this branch. Anywhere a status *enum*
string is exercised, it is clearly marked synthetic: BUILD_PLAN_DE.md §5's
enum table has never been seen on the wire, and the derivation
(``deliveredAt``/``hasDeliveryAttemptFailed``) is what actually governs
production behaviour — the maintainer's explicit build-past-the-gate
decision.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import GlsApiError, ParcelStatus
from custom_components.gls.countries import de as de_module
from custom_components.gls.countries.de import (
    async_get_parcel_de,
    map_parcel_status_de,
    normalize_parcel_de,
)


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep countries/de's one-shot WARNING dedup state isolated per test."""
    de_module._known_parcel_numbers.clear()
    de_module._recaptcha_warned = False
    de_module._unmapped_enum_logged.clear()
    de_module._unmapped_delivered_to_logged.clear()
    de_module._status_text_pairs_logged.clear()
    de_module._unexpected_keys_logged.clear()
    de_module._time_frame_type_logged = False
    yield
    de_module._known_parcel_numbers.clear()
    de_module._recaptcha_warned = False
    de_module._unmapped_enum_logged.clear()
    de_module._unmapped_delivered_to_logged.clear()
    de_module._status_text_pairs_logged.clear()
    de_module._unexpected_keys_logged.clear()
    de_module._time_frame_type_logged = False


# ---------------------------------------------------------------------------
# Fixture — the captured delivered-parcel body (germany.md, 2026-08-10)
# ---------------------------------------------------------------------------


def delivered_sample_de(tracking_reference: str = "075624238061") -> dict:
    """A trimmed, real (redacted) captured GLS-DE ``TrackingDetailsDto``.

    Wire-verified — the only DE body ever captured. No status field of any
    kind, and ``latestStatusText`` is the empty string.
    """
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "trackingReference": tracking_reference,
        "parcelNumber": "YOXVB8CE",
        "name": None,
        "parcelIcon": "PLAIN",
        "deliveryDirection": "UNKNOWN",
        "deliveredAt": "2026-05-31 15:39:01",
        "latestStatusText": "",
        "unlocked": False,
        "archived": True,
        "hasDeliveryAttemptFailed": False,
        "updatedAt": "2026-08-10 15:22:11",
        "addedAt": "2026-08-10 15:22:11",
        "deliveryEvents": [
            {
                "description": (
                    "Handing over the parcel to the recipient at the GLS "
                    "ParcelShop."
                ),
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-05-31 15:39:01",
            },
            {
                "description": (
                    "The parcel has been delivered at the ParcelShop (see "
                    "ParcelShop information)."
                ),
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-05-29 08:03:41",
            },
        ],
    }


def delivered_sample_de_with_status_text(
    tracking_reference: str = "REDACTED_TRACKING_REF",
) -> dict:
    """A third real (maintainer-redacted) captured body, 2026-08-11.

    Still delivered (``deliveredAt`` set) — does not satisfy BUILD_PLAN_DE.md
    §0's actual gate (an in-transit parcel). What it *does* show, unlike the
    first two captures: ``latestStatusText`` can be non-empty free text, so
    ``raw_status`` must use it directly rather than falling back to
    ``deliveryEvents[]``. All top-level keys and the general shape (no status
    enum field, ``unlocked: false``, no
    ``realTimeTrackingInformation``/``shopInformation``/
    ``consigneeInformation``/``senderInformation``) match the first two
    captures — see germany.md.
    """
    return {
        "id": "25b9837b-00c2-4338-9111-8ed2d769cf85",
        "trackingReference": tracking_reference,
        "parcelNumber": "REDACTED_PARCEL_NUMBER",
        "name": None,
        "parcelIcon": "PLAIN",
        "deliveryDirection": "UNKNOWN",
        "deliveredAt": "2026-08-10 14:46:27",
        "latestStatusText": "The parcel has been delivered / dropped off.",
        "unlocked": False,
        "archived": False,
        "hasDeliveryAttemptFailed": False,
        "updatedAt": "2026-08-11 11:16:25",
        "addedAt": "2026-08-11 11:16:25",
        "deliveryEvents": [
            {
                "description": "The parcel has been delivered / dropped off.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-10 14:46:27",
            },
            {
                "description": (
                    "The parcel is expected to be delivered during the day."
                ),
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-10 07:52:08",
            },
            {
                "description": "The parcel has reached the parcel center.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-07 01:46:37",
            },
            {
                "description": "The parcel has left the parcel center.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-07 01:53:35",
            },
            {
                "description": "The parcel has reached the parcel center.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-07 01:52:40",
            },
            {
                "description": "The parcel has left the parcel center.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-06 21:49:54",
            },
            {
                "description": "The parcel was handed over to GLS.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-06 21:44:02",
            },
            {
                "description": "The parcel has left the parcel center.",
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-06 21:40:10",
            },
            {
                "description": (
                    "The parcel data was entered into the GLS IT system; the "
                    "parcel was not yet handed over to GLS."
                ),
                "locationDetails": None,
                "coordinates": None,
                "occurrenceDateTime": "2026-08-05 15:01:08",
            },
        ],
    }


# ---------------------------------------------------------------------------
# map_parcel_status_de — derivation-first (§5)
# ---------------------------------------------------------------------------


def test_delivered_at_wins_over_a_synthetic_out_for_delivery_enum():
    """Derivation wins even when a (synthetic, never-observed) enum disagrees."""
    status = map_parcel_status_de(
        "OUT_FOR_DELIVERY",  # synthetic — never seen on the wire
        delivered_at="2026-05-31T15:39:01+00:00",
        has_delivery_attempt_failed=False,
    )
    assert status == ParcelStatus.DELIVERED


def test_delivered_at_wins_over_delivery_attempt_failed():
    """The band-ordering trap: both facts present, delivered_at is checked first."""
    status = map_parcel_status_de(
        None, delivered_at="2026-05-31T15:39:01+00:00", has_delivery_attempt_failed=True
    )
    assert status == ParcelStatus.DELIVERED


def test_delivery_attempt_failed_alone_is_problem():
    status = map_parcel_status_de(
        None, delivered_at=None, has_delivery_attempt_failed=True
    )
    assert status == ParcelStatus.PROBLEM


def test_no_signal_at_all_is_unknown():
    status = map_parcel_status_de(
        None, delivered_at=None, has_delivery_attempt_failed=False
    )
    assert status == ParcelStatus.UNKNOWN


def test_synthetic_enum_is_consulted_only_when_derivation_is_unknown(caplog):
    """The §5 override layer — clearly synthetic, not wire-verified."""
    status = map_parcel_status_de(
        "OUT_FOR_DELIVERY",
        delivered_at=None,
        has_delivery_attempt_failed=False,
    )
    assert status == ParcelStatus.OUT_FOR_DELIVERY


@pytest.mark.parametrize(
    "value,expected",
    [
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("NOT_DELIVERED", ParcelStatus.PROBLEM),
        ("BLOCKED", ParcelStatus.PROBLEM),
        ("UNKNOWN", ParcelStatus.UNKNOWN),
    ],
)
def test_synthetic_enum_table_values(value, expected):
    status = map_parcel_status_de(
        value, delivered_at=None, has_delivery_attempt_failed=False
    )
    assert status == expected


def test_synthetic_delivered_to_shop_variant_is_at_pickup_point():
    status = map_parcel_status_de(
        "DELIVERED_TO_PARCELSHOP",
        delivered_at=None,
        has_delivery_attempt_failed=False,
    )
    assert status == ParcelStatus.AT_PICKUP_POINT


def test_synthetic_delivered_to_neighbour_variant_is_delivered():
    status = map_parcel_status_de(
        "DELIVERED_TO_NEIGHBOUR",
        delivered_at=None,
        has_delivery_attempt_failed=False,
    )
    assert status == ParcelStatus.DELIVERED


def test_synthetic_delivered_to_unrecognised_member_defaults_delivered_and_warns(
    caplog,
):
    with caplog.at_level("WARNING"):
        status = map_parcel_status_de(
            "DELIVERED_TO_SOMETHING_NEW",
            delivered_at=None,
            has_delivery_attempt_failed=False,
        )
    assert status == ParcelStatus.DELIVERED
    assert any("DELIVERED_TO" in m for m in caplog.messages)


def test_unrecognised_enum_is_unknown_and_warns_once(caplog):
    with caplog.at_level("WARNING"):
        first = map_parcel_status_de(
            "SOMETHING_NEW", delivered_at=None, has_delivery_attempt_failed=False
        )
        second = map_parcel_status_de(
            "SOMETHING_NEW", delivered_at=None, has_delivery_attempt_failed=False
        )
    assert first == ParcelStatus.UNKNOWN
    assert second == ParcelStatus.UNKNOWN
    unmapped_warnings = [m for m in caplog.messages if "SOMETHING_NEW" in m]
    assert len(unmapped_warnings) == 1


# ---------------------------------------------------------------------------
# normalize_parcel_de
# ---------------------------------------------------------------------------


def test_normalize_captured_delivered_parcel():
    parcel = normalize_parcel_de(delivered_sample_de())
    assert parcel["carrier"] == "GLS"
    assert parcel["barcode"] == "075624238061"  # trackingReference, not parcelNumber
    assert parcel["status"] == ParcelStatus.DELIVERED  # via deliveredAt, not an enum
    # "" -> falls back to the newest deliveryEvents[].description (germany.md's
    # canonical mapping — "the fallback is not optional"; see the dedicated
    # fallback tests below for the empty/no-events edge cases).
    assert parcel["raw_status"] == (
        "Handing over the parcel to the recipient at the GLS ParcelShop."
    )
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-05-31T15:39:01+00:00"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["url"] == (
        "https://gls-group.com/GROUP/en/parcel-tracking?match=YOXVB8CE"
    )
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None  # opt-in, default off
    assert parcel["raw"]["parcelNumber"] == "YOXVB8CE"


def test_normalize_history_opt_in_is_reversed_to_oldest_first():
    parcel = normalize_parcel_de(delivered_sample_de(), include_history=True)
    assert len(parcel["history"]) == 2
    # Wire order is newest-first; canonical history is oldest-first.
    assert "delivered at the ParcelShop" in parcel["history"][0]["raw_status"]
    assert "Handing over" in parcel["history"][1]["raw_status"]
    assert parcel["history"][0]["timestamp"] == "2026-05-29T08:03:41+00:00"
    # §5: never infer status from localized event description text.
    assert parcel["history"][0]["status"] is None
    assert parcel["history"][1]["status"] is None


# ---------------------------------------------------------------------------
# raw_status fallback — germany.md: "latestStatusText, else the newest
# deliveryEvents[].description ... the fallback is not optional" (was
# missing; found via a third real capture with a non-empty latestStatusText,
# 2026-08-11).
# ---------------------------------------------------------------------------


def test_raw_status_falls_back_to_newest_event_description_when_empty():
    raw = delivered_sample_de()
    assert raw["latestStatusText"] == ""
    parcel = normalize_parcel_de(raw)
    # deliveryEvents[0] is the newest entry on the wire (pre-reversal).
    assert parcel["raw_status"] == raw["deliveryEvents"][0]["description"]


def test_raw_status_uses_non_empty_latest_status_text_directly():
    """The real 2026-08-11 capture: no fallback needed, latestStatusText wins."""
    raw = delivered_sample_de_with_status_text()
    parcel = normalize_parcel_de(raw)
    assert parcel["raw_status"] == "The parcel has been delivered / dropped off."
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-08-10T14:46:27+00:00"
    assert parcel["barcode"] == "REDACTED_TRACKING_REF"
    assert parcel["url"] == (
        "https://gls-group.com/GROUP/en/parcel-tracking?match="
        "REDACTED_PARCEL_NUMBER"
    )


def test_raw_status_stays_none_when_no_fallback_available():
    raw = delivered_sample_de()
    raw["latestStatusText"] = ""
    raw["deliveryEvents"] = []
    parcel = normalize_parcel_de(raw)
    assert parcel["raw_status"] is None


def test_raw_status_pairing_warning_survives_the_fallback(caplog):
    """The one-shot pairing log must not choke on a fallback-derived raw_status."""
    with caplog.at_level("WARNING"):
        normalize_parcel_de(delivered_sample_de())
        normalize_parcel_de(delivered_sample_de_with_status_text())
    pairing_warnings = [m for m in caplog.messages if "pairing" in m.lower()]
    assert len(pairing_warnings) == 2  # two distinct raw_status values


def test_normalize_delivery_attempt_failed_without_delivered_at_is_problem():
    raw = delivered_sample_de()
    raw["deliveredAt"] = None
    raw["hasDeliveryAttemptFailed"] = True
    parcel = normalize_parcel_de(raw)
    assert parcel["status"] == ParcelStatus.PROBLEM
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_normalize_no_signal_at_all_is_unknown():
    raw = delivered_sample_de()
    raw["deliveredAt"] = None
    raw["hasDeliveryAttemptFailed"] = False
    parcel = normalize_parcel_de(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False


def test_normalize_url_falls_back_to_tracking_reference_without_parcel_number():
    raw = delivered_sample_de()
    del raw["parcelNumber"]
    parcel = normalize_parcel_de(raw)
    assert parcel["url"] == (
        "https://gls-group.com/GROUP/en/parcel-tracking?match=075624238061"
    )


def test_normalize_sender_receiver_best_effort():
    raw = delivered_sample_de()
    raw["senderInformation"] = {"name": "Sender GmbH"}
    raw["consigneeInformation"] = {"name": "Empfänger"}
    parcel = normalize_parcel_de(raw)
    assert parcel["sender"] == "Sender GmbH"
    assert parcel["receiver"] == "Empfänger"


def test_normalize_pickup_shop_information():
    raw = delivered_sample_de()
    raw["shopInformation"] = {"name": "GLS ParcelShop Musterstraße"}
    parcel = normalize_parcel_de(raw)
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "GLS ParcelShop Musterstraße"


def test_normalize_pickup_shop_information_as_bare_string():
    raw = delivered_sample_de()
    raw["shopInformation"] = "GLS ParcelShop Musterstraße"
    parcel = normalize_parcel_de(raw)
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "GLS ParcelShop Musterstraße"


def test_normalize_unparseable_delivered_at_falls_back_to_none():
    raw = delivered_sample_de()
    raw["deliveredAt"] = "not-a-timestamp"
    parcel = normalize_parcel_de(raw)
    assert parcel["delivered_at"] is None
    assert parcel["delivered"] is False


def test_normalize_history_skips_events_with_unparseable_timestamp():
    raw = delivered_sample_de()
    raw["deliveryEvents"].append(
        {
            "description": "garbled",
            "locationDetails": None,
            "coordinates": None,
            "occurrenceDateTime": "not-a-timestamp",
        }
    )
    parcel = normalize_parcel_de(raw, include_history=True)
    assert len(parcel["history"]) == 2  # the garbled entry was skipped
    assert all(e["raw_status"] != "garbled" for e in parcel["history"])


def test_synthetic_delivered_to_unrecognised_member_warns_only_once(caplog):
    with caplog.at_level("WARNING"):
        map_parcel_status_de(
            "DELIVERED_TO_SOMETHING_NEW",
            delivered_at=None,
            has_delivery_attempt_failed=False,
        )
        map_parcel_status_de(
            "DELIVERED_TO_SOMETHING_NEW",
            delivered_at=None,
            has_delivery_attempt_failed=False,
        )
    warnings = [m for m in caplog.messages if "SOMETHING_NEW" in m]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# §6 WARNING obligations
# ---------------------------------------------------------------------------


def test_status_text_pairing_logged_once_per_distinct_value(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_de(delivered_sample_de())
        normalize_parcel_de(delivered_sample_de())  # same "" -> no second log
        other = delivered_sample_de()
        other["latestStatusText"] = "Unterwegs"
        other["deliveredAt"] = None
        normalize_parcel_de(other)
    pairing_warnings = [m for m in caplog.messages if "pairing" in m.lower()]
    assert len(pairing_warnings) == 2  # "" once, "Unterwegs" once


def test_unexpected_top_level_key_is_warned_once(caplog):
    raw = delivered_sample_de()
    raw["someNewField"] = 123
    with caplog.at_level("WARNING"):
        normalize_parcel_de(raw)
        normalize_parcel_de(raw)
    key_warnings = [m for m in caplog.messages if "someNewField" in m]
    assert len(key_warnings) == 1
    assert "someNewField: int" in key_warnings[0]


def test_known_dex_reconstructed_keys_do_not_warn(caplog):
    raw = delivered_sample_de()
    raw["shopInformation"] = {"name": "x"}
    raw["realTimeTrackingInformation"] = {}
    with caplog.at_level("WARNING"):
        normalize_parcel_de(raw)
    assert not any("unrecognised top-level" in m.lower() for m in caplog.messages)


def test_time_frame_type_is_warned_once_on_first_sight(caplog):
    raw = delivered_sample_de()
    raw["realTimeTrackingInformation"] = {"timeFrame": "12:00-14:00"}
    with caplog.at_level("WARNING"):
        normalize_parcel_de(raw)
        normalize_parcel_de(raw)
    frame_warnings = [m for m in caplog.messages if "timeFrame" in m]
    assert len(frame_warnings) == 1
    assert "type=str" in frame_warnings[0]


def test_no_time_frame_key_present_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_de(delivered_sample_de())
    assert not any("timeFrame" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Transport — async_get_parcel_de
# ---------------------------------------------------------------------------


def _ctx(status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    import json as _json

    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(
        return_value=_json.dumps(body) if body is not None else ""
    )
    response.headers = headers or {}
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_with(*responses: MagicMock) -> MagicMock:
    session = MagicMock()
    session.request = MagicMock(side_effect=list(responses))
    return session


def _de_session(token: str = "tok") -> MagicMock:
    de_session = MagicMock()
    de_session.async_get_token = AsyncMock(return_value=token)
    de_session.async_handle_unauthorized = AsyncMock(return_value="tok-refreshed")
    return de_session


async def test_first_fetch_adds_the_parcel_via_post():
    session = _session_with(_ctx(200, delivered_sample_de()))
    de_session = _de_session()
    body = await async_get_parcel_de(session, de_session, "075624238061", "00000")
    assert body["parcelNumber"] == "YOXVB8CE"
    call = session.request.call_args_list[0]
    assert call.args[0] == "POST"
    assert call.args[1] == de_module.GLS_DE_TRACKINGS_ADD_URL
    assert call.kwargs["json"] == {
        "trackingReference": "075624238061",
        "postcode": "00000",
    }
    assert call.kwargs["headers"]["Authorization"] == "Bearer tok"


async def test_second_fetch_polls_by_parcel_number_via_get():
    session = _session_with(
        _ctx(200, delivered_sample_de()),  # first: add
        _ctx(200, delivered_sample_de()),  # second: get-by-id
    )
    de_session = _de_session()
    await async_get_parcel_de(session, de_session, "075624238061", "00000")
    await async_get_parcel_de(session, de_session, "075624238061", "00000")
    assert session.request.call_count == 2
    second_call = session.request.call_args_list[1]
    assert second_call.args[0] == "GET"
    assert "YOXVB8CE" in second_call.args[1]


async def test_add_returns_none_on_semantic_404():
    session = _session_with(_ctx(404, {"displayableMessage": "not found"}))
    result = await async_get_parcel_de(session, _de_session(), "999999999999", "00000")
    assert result is None


async def test_add_409_without_a_cached_parcel_number_raises():
    session = _session_with(_ctx(409, {"displayableMessage": "already tracking"}))
    with pytest.raises(GlsApiError):
        await async_get_parcel_de(session, _de_session(), "075624238061", "00000")


async def test_add_raises_on_unexpected_status():
    session = _session_with(_ctx(500))
    with pytest.raises(GlsApiError):
        await async_get_parcel_de(session, _de_session(), "075624238061", "00000")


async def test_get_by_parcel_number_404_returns_none():
    session = _session_with(
        _ctx(200, delivered_sample_de()),  # add, caches parcelNumber
        _ctx(404),  # subsequent get-by-id: gone
    )
    de_session = _de_session()
    await async_get_parcel_de(session, de_session, "075624238061", "00000")
    result = await async_get_parcel_de(session, de_session, "075624238061", "00000")
    assert result is None


async def test_401_triggers_one_refresh_and_retry():
    session = _session_with(
        _ctx(401),
        _ctx(200, delivered_sample_de()),
    )
    de_session = _de_session()
    body = await async_get_parcel_de(session, de_session, "075624238061", "00000")
    assert body["parcelNumber"] == "YOXVB8CE"
    de_session.async_handle_unauthorized.assert_awaited_once()
    retry_call = session.request.call_args_list[1]
    assert retry_call.kwargs["headers"]["Authorization"] == "Bearer tok-refreshed"


def _ctx_raw_text(status: int, text: str) -> MagicMock:
    """Like ``_ctx`` but with a literal (possibly non-JSON) body."""
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    response.headers = {}
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_add_unparseable_body_on_200_is_treated_as_no_body():
    session = _session_with(_ctx_raw_text(200, "not json"))
    de_session = _de_session()
    body = await async_get_parcel_de(session, de_session, "075624238061", "00000")
    assert body is None


async def test_get_by_parcel_number_raises_on_unexpected_status():
    session = _session_with(
        _ctx(200, delivered_sample_de()),  # add, caches parcelNumber
        _ctx(500),  # subsequent get-by-id
    )
    de_session = _de_session()
    await async_get_parcel_de(session, de_session, "075624238061", "00000")
    with pytest.raises(GlsApiError):
        await async_get_parcel_de(session, de_session, "075624238061", "00000")


async def test_recaptcha_mention_is_warned_once(caplog):
    session = _session_with(
        _ctx(403, {"displayableMessage": "please solve the recaptcha"}),
        _ctx(403, {"displayableMessage": "please solve the recaptcha"}),
    )
    de_session = _de_session()
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsApiError):
            await async_get_parcel_de(session, de_session, "075624238061", "00000")
        de_module._known_parcel_numbers.pop("075624238061", None)
        with pytest.raises(GlsApiError):
            await async_get_parcel_de(session, de_session, "075624238061", "00000")
    recaptcha_warnings = [m for m in caplog.messages if "recaptcha" in m.lower()]
    assert len(recaptcha_warnings) == 1
