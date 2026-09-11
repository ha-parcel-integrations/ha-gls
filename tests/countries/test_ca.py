"""Tests for the GLS Canada / Dicom transport and mapping."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import GlsApiError, ParcelStatus
from custom_components.gls.countries.ca import (
    async_get_parcel_ca,
    map_event_status_ca,
    map_parcel_status_ca,
    normalize_parcel_ca,
)

TRACKING_NO = "CA12345678"


def _response_session(*responses: tuple[int, dict | None]) -> MagicMock:
    session = MagicMock()
    contexts = []
    for status, body in responses:
        response = AsyncMock()
        response.status = status
        response.json = AsyncMock(return_value=body)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    session.get = MagicMock(side_effect=contexts)
    return session


def delivered_sample() -> dict:
    """A structural, synthetic CA response including fields retained in raw."""
    return {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "sender": {
                    "city": "Exampleville",
                    "provinceCode": "ON",
                    "countryCode": "CA",
                },
                "consignee": {"city": "Receiver City", "provinceCode": "ON"},
                "currentStatus": {"name": "Delivered", "date": "2026-08-28 14:52:32"},
                "imageStatus": [{"name": "delivery", "status": "current"}],
                "totalWeight": 4.54,
                "unitOfMeasurement": "kg | cm",
                "parcels": [
                    {
                        "parcelId": 1,
                        "length": 20.32,
                        "width": 10.16,
                        "height": 10.16,
                        "activities": [
                            {
                                "activityDate": "2026-08-28 08:12:48",
                                "status": "On Delivery",
                                "statusDetail": "Synthetic delivery detail",
                                "terminal": "Example terminal",
                            },
                            {
                                "activityDate": "2026-08-28 14:52:32",
                                "status": "Delivered",
                                "statusDetail": "Synthetic delivered detail",
                            },
                        ],
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Shipment Created", ParcelStatus.REGISTERED),
        ("In Transit", ParcelStatus.IN_TRANSIT),
        ("On Delivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivered", ParcelStatus.DELIVERED),
    ],
)
def test_status_mapping(value, expected):
    assert map_parcel_status_ca(value) == expected


def test_unknown_event_status_stays_none():
    assert map_parcel_status_ca("Uncaptured status") == ParcelStatus.UNKNOWN
    assert map_event_status_ca("Uncaptured status") is None


def test_normalizer_preserves_full_raw_envelope_and_builds_history():
    raw = delivered_sample()
    parcel = normalize_parcel_ca(raw, parcel_no=TRACKING_NO, include_history=True)

    assert parcel["barcode"] == TRACKING_NO
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-08-28T14:52:32+00:00"
    assert parcel["weight"] == 4.54
    assert parcel["dimensions"]["text"] == "20.32 x 10.16 x 10.16 cm"
    assert [event["status"] for event in parcel["history"]] == [
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert parcel["sender"] == "Exampleville, ON, CA"
    assert parcel["receiver"] == "Receiver City, ON"
    assert parcel["url"] == (
        "https://gls-group.com/CA/en/send-and-receive/track-a-shipment/"
        f"?match={TRACKING_NO}"
    )
    assert parcel["raw"] is raw
    assert parcel["raw"]["shipments"][0]["consignee"]["city"] == "Receiver City"


async def test_transport_prefers_postcode_details_route():
    body = delivered_sample()
    session = _response_session((200, body))

    assert await async_get_parcel_ca(session, TRACKING_NO, "K1A 0B1") is body
    url = session.get.call_args.args[0]
    assert url.endswith(f"/K1A0B1/{TRACKING_NO}")
    assert session.get.call_args.kwargs["headers"] == {"Accept-Language": "en-CA"}


async def test_transport_returns_none_for_no_record_without_fallback():
    body = {
        "shipments": [
            {"trackingNumber": TRACKING_NO, "currentStatus": {"name": "NORECORD"}}
        ]
    }
    session = _response_session((200, body))

    assert await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1") is None
    assert session.get.call_count == 1


@pytest.mark.parametrize("detail_status", [401, 403, 404])
async def test_transport_falls_back_to_code_only_when_detail_is_rejected(detail_status):
    body = delivered_sample()
    session = _response_session((detail_status, None), (200, body))

    assert await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1") is body
    assert session.get.call_count == 2
    assert session.get.call_args.args[0].endswith(f"/{TRACKING_NO}")


def test_unmapped_status_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_ca("Held At Depot") == ParcelStatus.UNKNOWN
        assert map_event_status_ca("Held At Depot") is None
    assert sum("Unrecognised GLS Canada status" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_blank_status_is_neutral_without_warning(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_ca(value) == ParcelStatus.UNKNOWN
        assert map_event_status_ca(value) is None
    assert not caplog.records


def test_unparseable_timestamps_are_dropped_and_warned_once(caplog):
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "currentStatus": {"name": "In Transit", "date": "28/08/2026"},
                "parcels": [
                    "not-a-dict",
                    {
                        "activities": [
                            "not-a-dict",
                            {"activityDate": "28/08/2026", "status": "In Transit"},
                            {"activityDate": None, "status": "In Transit"},
                            {"activityDate": "2026-08-28 08:12:48", "status": "In Transit"},
                        ]
                    }
                ],
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_ca(raw, parcel_no=TRACKING_NO, include_history=True)

    assert [event["timestamp"] for event in parcel["history"]] == [
        "2026-08-28T08:12:48+00:00"
    ]
    assert parcel["delivered_at"] is None
    assert sum("unparseable timestamp" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"shipments": "not-a-list"},
        {"shipments": ["not-a-dict"]},
        {"shipments": [{"trackingNumber": "SOMEONEELSE"}]},
        {},
    ],
)
def test_unexpected_envelope_shapes_normalize_to_an_empty_parcel(raw, caplog):
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_ca(raw, parcel_no=TRACKING_NO)

    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["barcode"] == TRACKING_NO
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


def test_shape_warning_is_logged_once_per_marker(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_parcel_ca({"shipments": "not-a-list"}, parcel_no=TRACKING_NO)
        normalize_parcel_ca({"shipments": "also-not-a-list"}, parcel_no=TRACKING_NO)
    assert sum("unexpected field shape" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize(
    ("parcels", "expected"),
    [
        # Colli with differing dimensions have no single shipment size.
        (
            [
                {"length": 20.0, "width": 10.0, "height": 10.0},
                {"length": 30.0, "width": 10.0, "height": 10.0},
            ],
            None,
        ),
        ([{"length": None, "width": None, "height": None}], None),
        ([], None),
        ("not-a-list", None),
    ],
)
def test_dimensions_edge_cases(parcels, expected):
    raw = {"shipments": [{"trackingNumber": TRACKING_NO, "parcels": parcels}]}
    assert normalize_parcel_ca(raw, parcel_no=TRACKING_NO)["dimensions"] == expected


def test_partial_dimensions_keep_the_numbers_but_drop_the_label():
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "parcels": [{"length": 20.0, "width": None, "height": 10.0}],
            }
        ]
    }
    assert normalize_parcel_ca(raw, parcel_no=TRACKING_NO)["dimensions"] == {
        "length": 20.0,
        "width": None,
        "height": 10.0,
        "text": None,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Acme Ltd", "Acme Ltd"),
        ("", None),
        ({"name": "Acme Ltd", "city": "Exampleville"}, "Acme Ltd"),
        ({"city": "Exampleville", "countryCode": "CA"}, "Exampleville, CA"),
        ({}, None),
        (None, None),
    ],
)
def test_party_label_variants(value, expected):
    raw = {"shipments": [{"trackingNumber": TRACKING_NO, "sender": value}]}
    assert normalize_parcel_ca(raw, parcel_no=TRACKING_NO)["sender"] == expected


async def test_transport_raises_on_a_server_error_instead_of_falling_back():
    session = _response_session((500, None))
    with pytest.raises(GlsApiError):
        await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1")
    assert session.get.call_count == 1


async def test_transport_raises_when_the_code_only_fallback_also_fails():
    session = _response_session((404, None), (500, None))
    with pytest.raises(GlsApiError):
        await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1")
    assert session.get.call_count == 2


async def test_transport_returns_none_for_an_unparseable_json_body(caplog):
    session = _response_session((200, None))
    session.get.side_effect = None
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(side_effect=ValueError("not json"))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)

    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1") is None
    assert any("unparseable JSON body" in r.message for r in caplog.records)


async def test_transport_returns_none_for_a_non_object_body(caplog):
    session = _response_session((200, ["not", "an", "object"]))
    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1") is None
    assert any("unexpected field shape" in r.message for r in caplog.records)


async def test_transport_returns_none_when_no_shipment_matches_the_code():
    session = _response_session(
        (200, {"shipments": [{"trackingNumber": "SOMEONEELSE"}]})
    )
    assert await async_get_parcel_ca(session, TRACKING_NO, "K1A0B1") is None
