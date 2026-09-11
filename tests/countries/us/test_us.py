"""Tests for the GLS US transport and mapping."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import GlsApiError, ParcelStatus
from custom_components.gls.countries.us import (
    async_get_parcel_us,
    map_event_status_us,
    map_parcel_status_us,
    normalize_parcel_us,
)

TRACKING_NO = "12345678901234"


def _response_session(status: int, body: object) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=context)
    return session


def delivered_sample() -> dict:
    """A structural, synthetic US response including fields retained in raw."""
    return {
        "hasMultipleAccounts": False,
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "controlNumber": "0000001",
                "status": "SHIPMENT DELIVERED",
                "shipDate": "2026-09-07T00:00:00",
                "estimatedDeliveryDate": "2026-09-09T00:00:00",
                "deliveryDate": "2026-09-09T11:04:00",
                "deliveredAt": "Front door",
                "signedBy": "Synthetic signer",
                "serviceType": "Ground",
                "deliveryAddress": "1 Example Street, Exampleville",
                "transitDetails": [
                    {
                        "eventDateTime": "2026-09-08T02:15:00",
                        "eventDetails": "IN TRANSIT",
                        "location": "Example Hub",
                    },
                    {
                        "eventDateTime": "2026-09-09T11:04:00",
                        "eventDetails": "SHIPMENT DELIVERED",
                        "location": "Exampleville",
                    },
                ],
            }
        ],
    }


def test_delivered_literal_maps_exactly():
    assert map_parcel_status_us("SHIPMENT DELIVERED") == ParcelStatus.DELIVERED
    assert map_event_status_us("Delivered") == ParcelStatus.DELIVERED


def test_normalizer_preserves_full_raw_envelope_and_builds_history():
    raw = delivered_sample()
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO, include_history=True)

    assert parcel["barcode"] == TRACKING_NO
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "SHIPMENT DELIVERED"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-09-09T11:04:00+00:00"
    # No weight/dimensions/window field exists on this backend.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    # The delivery address is not a party name — it stays in raw only.
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["url"] == (
        f"https://gls-group.com/GROUP/en/parcel-tracking?match={TRACKING_NO}"
    )
    assert [event["status"] for event in parcel["history"]] == [
        None,
        ParcelStatus.DELIVERED,
    ]
    assert [event["timestamp"] for event in parcel["history"]] == [
        "2026-09-08T02:15:00+00:00",
        "2026-09-09T11:04:00+00:00",
    ]
    assert parcel["raw"] is raw
    assert parcel["raw"]["shipments"][0]["deliveryAddress"]


def test_newest_shipment_wins_over_array_position():
    """One barcode can hold a return plus its replacement, oldest first."""
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "RETURNED TO SENDER",
                "shipDate": "2026-08-01T00:00:00",
                "transitDetails": [
                    {
                        "eventDateTime": "2026-08-04T09:00:00",
                        "eventDetails": "RETURNED TO SENDER",
                    }
                ],
            },
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveryDate": "2026-09-09T11:04:00",
                "transitDetails": [
                    {
                        "eventDateTime": "2026-09-09T11:04:00",
                        "eventDetails": "SHIPMENT DELIVERED",
                    }
                ],
            },
        ]
    }
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "SHIPMENT DELIVERED"


def test_a_reversed_response_order_still_selects_the_newest():
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveryDate": "2026-09-09T11:04:00",
            },
            {
                "trackingNumber": TRACKING_NO,
                "status": "PICKED UP",
                "shipDate": "2026-08-01T00:00:00",
            },
        ]
    }
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["raw_status"] == "SHIPMENT DELIVERED"


def test_sentinel_dates_never_make_a_record_look_current():
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveryDate": "2026-09-09T11:04:00",
            },
            {
                "trackingNumber": TRACKING_NO,
                "status": "UNKNOWN RECORD",
                "shipDate": "9999-01-01T00:00:00",
                "deliveryDate": "0001-01-01T00:00:00",
                "transitDetails": [
                    {"eventDateTime": "1900-01-01T00:00:00", "eventDetails": "NONE"}
                ],
            },
        ]
    }
    raw["shipments"][1]["shipDate"] = "0001-01-01T00:00:00"
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["raw_status"] == "SHIPMENT DELIVERED"


@pytest.mark.parametrize(
    ("shipment", "expected"),
    [
        (
            {"status": "ON THE WAY", "deliveryDate": "2026-09-09T11:04:00"},
            ParcelStatus.DELIVERED,
        ),
        (
            {
                "status": "ON THE WAY",
                "shipDate": "2026-09-07T00:00:00",
                "transitDetails": [
                    {"eventDateTime": "2026-09-08T02:15:00", "eventDetails": "SCAN"}
                ],
            },
            ParcelStatus.IN_TRANSIT,
        ),
        ({"status": "ON THE WAY", "shipDate": "2026-09-07T00:00:00"}, ParcelStatus.REGISTERED),
        ({"status": "ON THE WAY"}, ParcelStatus.UNKNOWN),
    ],
)
def test_an_uncaptured_status_is_derived_from_the_shipment_dates(shipment, expected):
    raw = {"shipments": [{"trackingNumber": TRACKING_NO, **shipment}]}
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["status"] == expected
    # The carrier's own wording survives for the unrecognised-status report.
    assert parcel["raw_status"] == "ON THE WAY"


def test_a_derived_delivery_uses_the_last_scan_when_there_is_no_delivery_date():
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveredAt": "Front door",
                "transitDetails": [
                    {
                        "eventDateTime": "2026-09-09T11:04:00",
                        "eventDetails": "SHIPMENT DELIVERED",
                    }
                ],
            }
        ]
    }
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["delivered_at"] == "2026-09-09T11:04:00+00:00"


def test_unmapped_status_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_us("Held At Depot") == ParcelStatus.UNKNOWN
        assert map_event_status_us("Held At Depot") is None
    assert sum("Unrecognised GLS US status" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_blank_status_is_neutral_without_warning(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_us(value) == ParcelStatus.UNKNOWN
        assert map_event_status_us(value) is None
    assert not caplog.records


def test_unparseable_timestamps_are_dropped_and_warned_once(caplog):
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveryDate": "09/09/2026",
                "transitDetails": [
                    "not-a-dict",
                    {"eventDateTime": "09/09/2026", "eventDetails": "SCAN"},
                    {"eventDateTime": None, "eventDetails": "SCAN"},
                    {"eventDateTime": "2026-09-08T02:15:00", "eventDetails": "SCAN"},
                ],
            }
        ]
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO, include_history=True)

    assert [event["timestamp"] for event in parcel["history"]] == [
        "2026-09-08T02:15:00+00:00"
    ]
    # Delivered with no usable delivery date falls back to the last scan.
    assert parcel["delivered_at"] == "2026-09-08T02:15:00+00:00"
    assert sum("unparseable timestamp" in r.message for r in caplog.records) == 1


def test_an_offset_bearing_timestamp_is_kept_as_sent():
    raw = {
        "shipments": [
            {
                "trackingNumber": TRACKING_NO,
                "status": "SHIPMENT DELIVERED",
                "deliveryDate": "2026-09-09T11:04:00-04:00",
            }
        ]
    }
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["delivered_at"] == "2026-09-09T11:04:00-04:00"


@pytest.mark.parametrize(
    "raw",
    [
        {"shipments": "not-a-list"},
        {"shipments": ["not-a-dict"]},
        {"shipments": [{"trackingNumber": "SOMEONEELSE"}]},
        {},
    ],
)
def test_unexpected_envelope_shapes_normalize_to_an_empty_parcel(raw):
    parcel = normalize_parcel_us(raw, parcel_no=TRACKING_NO)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["barcode"] == TRACKING_NO
    assert parcel["raw_status"] is None


def test_shape_warning_is_logged_once_per_marker(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_parcel_us({"shipments": "not-a-list"}, parcel_no=TRACKING_NO)
        normalize_parcel_us({"shipments": "also-not-a-list"}, parcel_no=TRACKING_NO)
        normalize_parcel_us(
            {"shipments": [{"trackingNumber": TRACKING_NO, "transitDetails": {}}]},
            parcel_no=TRACKING_NO,
            include_history=True,
        )
    assert sum("unexpected field shape" in r.message for r in caplog.records) == 2


async def test_transport_posts_the_tracking_number_without_the_postcode():
    body = delivered_sample()
    session = _response_session(200, body)

    assert await async_get_parcel_us(session, TRACKING_NO, "90210") is body
    url = session.post.call_args.args[0]
    assert url == (
        "https://connect.gls-us.com/api/public/tracking/"
        "TrackShipmentSummariesByTrackingNumbers"
    )
    assert session.post.call_args.kwargs["json"] == {
        "trackingNumbers": TRACKING_NO,
        "isFreight": False,
    }
    assert "90210" not in str(session.post.call_args)


async def test_transport_treats_a_204_as_not_found():
    session = _response_session(204, None)
    assert await async_get_parcel_us(session, TRACKING_NO, "90210") is None


async def test_transport_returns_none_when_no_shipment_matches_the_code():
    session = _response_session(200, {"shipments": [{"trackingNumber": "OTHER"}]})
    assert await async_get_parcel_us(session, TRACKING_NO, "90210") is None


async def test_transport_raises_on_a_server_error():
    session = _response_session(500, None)
    with pytest.raises(GlsApiError):
        await async_get_parcel_us(session, TRACKING_NO, "90210")


async def test_transport_returns_none_for_an_unparseable_json_body(caplog):
    session = _response_session(200, None)
    response = await session.post.return_value.__aenter__()
    response.json = AsyncMock(side_effect=ValueError("not json"))

    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_us(session, TRACKING_NO, "90210") is None
    assert any("unparseable JSON body" in r.message for r in caplog.records)


async def test_transport_returns_none_for_a_non_object_body(caplog):
    session = _response_session(200, ["not", "an", "object"])
    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_us(session, TRACKING_NO, "90210") is None
    assert any("unexpected field shape" in r.message for r in caplog.records)
