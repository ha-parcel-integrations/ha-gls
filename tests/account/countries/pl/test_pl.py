"""Polish account normalizer and feed validation — synthetic parcels only."""

from __future__ import annotations

import pytest

from custom_components.gls.account.countries import ACCOUNT_SOURCES
from custom_components.gls.account.countries.pl import (
    normalize_parcel_pl_account,
    normalize_phone_pl,
    parse_feed_pl,
)
from custom_components.gls.account.countries.pl.session import DETAILS_KEY, GlsPlSession
from custom_components.gls.account.errors import GlsAccountFeedError
from custom_components.gls.const import ParcelStatus


def _details(*, weight=2.69, pin="9999", statuses=None):
    """A scrubbed details payload; ``pin`` is here to prove it is dropped."""
    if statuses is None:
        statuses = [
            {
                "packageStatusName": "Doręczona",
                "packageStatusDescription": "Paczka doręczona",
                "packageStatusDate": "2026-11-16T13:03:00.871Z",
                "packageStatusPlace": "Gdansk PL8300",
            },
            {
                "packageStatusName": "W doręczeniu",
                "packageStatusDescription": "Paczka w doręczeniu",
                "packageStatusDate": "2026-11-16T07:24:09.720229Z",
                "packageStatusPlace": None,
            },
        ]
    return {
        "trackingShipment": {
            "trackingUid": "00000000-0000-0000-0000-000000000001",
            "shipmentNo": "490000000001",
            "weight": weight,
            "pin": pin,
            "reference": "EXREF0000000001",
            "deliveryMethod": "TO_DOOR",
            "courierPhoneNumber": "+48000000000",
            "proofOfDelivery": {
                "receiverName": "Example",
                "proofOfDeliveryMethod": "SIGNATURE",
            },
        },
        "trackingShipmentPackages": [
            {
                "packageUid": "00000000-0000-0000-0000-000000000002",
                "packageNo": "490000000001",
                "state": "DELIVERED",
                "weight": weight,
                "packageStatuses": statuses,
            }
        ],
    }


def feed_item(number="490000000001", *, ident="DELIVERED", details=True, **overrides):
    item = {
        "trackingUid": "00000000-0000-0000-0000-000000000001",
        "shipmentNo": number,
        "trackingId": "AB1C234D",
        "stateDate": "2026-11-16T13:03:00.871Z",
        "packageAmount": 1,
        "progressBar": "Doręczona",
        "progressBarIdent": ident,
        "receiverPostalCode": "00-001",
        "senderName": "Example Sender",
        "receiverName": "Example Recipient",
        "customName": None,
        "isManual": False,
        "deliveryCountryCode": "PL",
        **overrides,
    }
    if details:
        item[DETAILS_KEY] = _details()
    return item


def test_normalize_maps_status_and_barcode_from_feed():
    parcel = normalize_parcel_pl_account(feed_item(ident="INTRANSIT"))
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["barcode"] == "490000000001"
    assert parcel["sender"] == "Example Sender"
    assert parcel["receiver"] == "Example Recipient"
    assert parcel["raw_status"] == "Doręczona"
    assert parcel["delivered"] is False
    assert parcel["url"].endswith("match=490000000001")


def test_normalize_delivered_sets_flag_and_moment():
    parcel = normalize_parcel_pl_account(feed_item(ident="DELIVERED"))
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-11-16T13:03:00.871000+00:00"


def test_normalize_pickup_point_status():
    parcel = normalize_parcel_pl_account(feed_item(ident="DELIVEREDPS"))
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True


def test_weight_comes_from_details():
    assert normalize_parcel_pl_account(feed_item())["weight"] == 2.69
    assert normalize_parcel_pl_account(feed_item(details=False))["weight"] is None


def test_history_is_built_only_when_requested_and_sorted():
    assert normalize_parcel_pl_account(feed_item())["history"] is None
    history = normalize_parcel_pl_account(feed_item(), include_history=True)["history"]
    assert [event["status"] for event in history] == [
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert history[0]["raw_status"] == "W doręczeniu"


def test_raw_is_the_full_record_minus_pin_and_links():
    item = feed_item()
    item["trackingLink"] = "https://example.invalid/track"
    item[DETAILS_KEY]["trackingShipment"]["podImageUrl"] = "https://example.invalid/p"
    parcel = normalize_parcel_pl_account(item, include_history=True)
    raw = parcel["raw"]
    shipment = raw[DETAILS_KEY]["trackingShipment"]
    assert "pin" not in shipment
    assert "podImageUrl" not in shipment
    assert "trackingLink" not in raw
    # Everything else survives, nested values and identifiers included.
    assert raw["shipmentNo"] == "490000000001"
    assert raw["trackingUid"] == item["trackingUid"]
    assert raw["receiverPostalCode"] == "00-001"
    assert shipment["weight"] == 2.69
    assert shipment["courierPhoneNumber"] == "+48000000000"
    assert shipment["proofOfDelivery"]["receiverName"] == "Example"
    assert raw[DETAILS_KEY]["trackingShipmentPackages"][0]["packageStatuses"]
    # The input is not mutated.
    assert item[DETAILS_KEY]["trackingShipment"]["pin"] == "9999"


def test_non_finite_number_is_dropped_from_raw():
    parcel = normalize_parcel_pl_account(feed_item(packageAmount=float("inf")))
    assert "packageAmount" not in parcel["raw"]


def test_unknown_status_without_history_is_unknown_not_an_error():
    parcel = normalize_parcel_pl_account(
        feed_item(ident="SOMETHING_NEW", details=False)
    )
    assert parcel["status"] == ParcelStatus.UNKNOWN


def test_unknown_status_falls_back_to_the_newest_mapped_event():
    """The reused mapper's warning promises this fallback, so it must happen."""
    parcel = normalize_parcel_pl_account(feed_item(ident="SOMETHING_NEW"))
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_fallback_skips_newer_unmapped_and_undated_events():
    item = feed_item(ident="SOMETHING_NEW")
    item[DETAILS_KEY] = _details(
        statuses=[
            {
                "packageStatusName": "W doręczeniu",
                "packageStatusDate": "2026-11-16T07:00:00Z",
            },
            {
                "packageStatusName": "Zupełnie nowy",
                "packageStatusDate": "2026-11-16T09:00:00Z",
            },
            {"packageStatusName": "Doręczona", "packageStatusDate": None},
        ]
    )
    parcel = normalize_parcel_pl_account(item)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_pickup_point_is_never_guessed():
    """``parcelShop`` has never been seen populated, so nothing is parsed from it."""
    item = feed_item(ident="DELIVEREDPS")
    item[DETAILS_KEY]["trackingShipment"]["parcelShop"] = {"name": "Example Shop"}
    assert normalize_parcel_pl_account(item)["pickup_point"] is None


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("600000000", "600000000"),
        (" 600 000 000 ", "600000000"),
        ("600-000-000", "600000000"),
        ("+48 600 000 000", "600000000"),
        ("0048600000000", "600000000"),
        ("(+48) 600.000.000", "600000000"),
    ],
)
def test_phone_is_normalized_to_the_nine_digit_form(typed, stored):
    assert normalize_phone_pl(typed) == stored


@pytest.mark.parametrize(
    "typed",
    ["", "60000000", "6000000000", "+49600000000", "60000000a", "６００００００００"],
)
def test_phone_that_is_not_nine_polish_digits_is_rejected(typed):
    assert normalize_phone_pl(typed) is None


def test_parse_feed_returns_number_raw_pairs():
    feed = {"items": [feed_item()], "totalElements": 1}
    entries = parse_feed_pl(feed, None)
    assert entries[0][0] == "490000000001"
    assert entries[0][1]["progressBarIdent"] == "DELIVERED"


def test_parse_feed_empty_is_valid():
    assert parse_feed_pl({"items": [], "totalElements": 0}, None) == []


@pytest.mark.parametrize(
    "feed",
    [
        "not a dict",
        {"items": "nope", "totalElements": 0},
        {"items": [feed_item()], "totalElements": 2},  # incomplete
        {"items": [feed_item()], "totalElements": True},  # bool count
        {"items": [{"shipmentNo": ""}], "totalElements": 1},  # blank number
    ],
)
def test_parse_feed_rejects_invalid_or_incomplete(feed):
    with pytest.raises(GlsAccountFeedError):
        parse_feed_pl(feed, None)


def test_registry_restores_a_pl_session_without_its_phone():
    """Refresh credentials restore; the phone is learned at sign-in only."""
    session = ACCOUNT_SOURCES["PL"].create_session(
        None, {"uid": "u", "phone": "600000000", "refresh_token": "r"}, None
    )
    assert isinstance(session, GlsPlSession)
    assert session.uid == "u" and session.refresh_token == "r"
    assert session.phone is None


def test_parse_feed_rejects_non_dict_item():
    with pytest.raises(GlsAccountFeedError):
        parse_feed_pl({"items": ["nope"], "totalElements": 1}, None)


def test_malformed_details_degrade_gracefully():
    item = feed_item()
    item[DETAILS_KEY] = {
        "trackingShipment": {"weight": "heavy"},  # non-numeric weight
        "trackingShipmentPackages": ["not-a-dict", {"packageStatuses": "x"}],
    }
    parcel = normalize_parcel_pl_account(item, include_history=True)
    assert parcel["weight"] is None
    assert parcel["history"] == []


def test_parse_feed_repeated_shipment_keeps_first_and_keeps_publishing():
    first = feed_item(ident="INTRANSIT")
    repeat = feed_item(ident="DELIVERED")
    other = feed_item("490000000002")
    entries = parse_feed_pl({"items": [first, repeat, other], "totalElements": 3}, None)
    assert [number for number, _ in entries] == ["490000000001", "490000000002"]
    assert entries[0][1]["progressBarIdent"] == "INTRANSIT"


def test_delivered_at_only_for_a_delivered_parcel():
    """A "delivered" event on a parcel the feed still calls in transit is no delivery."""
    parcel = normalize_parcel_pl_account(feed_item(ident="INTRANSIT"))
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_delivered_parcel_without_details_falls_back_to_state_date():
    parcel = normalize_parcel_pl_account(feed_item(ident="DELIVERED", details=False))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-11-16T13:03:00.871000+00:00"
