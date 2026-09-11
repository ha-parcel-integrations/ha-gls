"""Tests for the GLS Poland national myGLS transport and mapping."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import GlsApiError, ParcelStatus
from custom_components.gls.countries.pl import (
    async_get_parcel_pl,
    map_event_status_pl,
    map_parcel_status_pl,
    normalize_parcel_pl,
)

# Synthetic: the short form a Polish user is given, and the carrier's own
# form with its extra check digit. Both resolve on this route.
SHORT_NO = "99900011122"
LONG_NO = "999000111223"


def _session(status: int, body: object) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=context)
    return session


def _event(name: str, description: str, when: str, place: str = "PL1700") -> dict:
    return {
        "packageStatusUid": None,
        "packageStatusName": name,
        "packageStatusDescription": description,
        "packageStatusDate": when,
        "packageStatusPlace": place,
    }


def delivered_sample() -> dict:
    """A structural sample mirroring a real delivered Polish parcel."""
    return {
        "parcelNumber": LONG_NO,
        "stateDate": "2026-08-06T08:24:05.3434+02:00",
        "progressBar": "Doręczona",
        "progressBarIdent": "DELIVERED",
        "hasDeliveryCode": False,
        # Newest first, as the carrier sends them.
        "eventReasons": [
            _event("Doręczona", "Paczka doręczona ", "2026-08-06T08:24:05.3434+02:00"),
            _event("W doręczeniu", "Paczka w doręczeniu ", "2026-08-06T06:33:12.8738+02:00"),
            _event(
                "W drodze",
                "Paczka zarejestrowana w filii GLS ",
                "2026-08-06T06:31:39.476+02:00",
            ),
            _event(
                "Utworzenie paczki",
                "Nadawca nadał numer paczce",
                "2026-08-04T08:56:34+02:00",
                "PL1400",
            ),
        ],
    }


@pytest.mark.parametrize(
    ("ident", "expected"),
    [
        ("PREADVICE", ParcelStatus.REGISTERED),
        ("INTRANSIT", ParcelStatus.IN_TRANSIT),
        ("INDELIVERY", ParcelStatus.OUT_FOR_DELIVERY),
        ("DELIVEREDPS", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("NOTPICKEDUP", ParcelStatus.RETURNING),
        ("NOTDELIVERED", ParcelStatus.PROBLEM),
    ],
)
def test_shipment_status_mapping(ident, expected):
    assert map_parcel_status_pl(ident) == expected


def test_deliveredps_is_not_delivered():
    """DELIVEREDPS contains the substring DELIVERED — matching must be exact."""
    assert map_parcel_status_pl("DELIVEREDPS") == ParcelStatus.AT_PICKUP_POINT
    raw = {"progressBarIdent": "DELIVEREDPS", "eventReasons": []}
    parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO)
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["pickup"] is True


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Utworzenie paczki", ParcelStatus.REGISTERED),
        ("W drodze", ParcelStatus.IN_TRANSIT),
        ("W doręczeniu", ParcelStatus.OUT_FOR_DELIVERY),
        ("Gotowa do odbioru", ParcelStatus.AT_PICKUP_POINT),
        ("Doręczona", ParcelStatus.DELIVERED),
        # "niedoręczona" contains "doręczona": exact match only.
        ("Niedoręczona", ParcelStatus.PROBLEM),
    ],
)
def test_event_text_mapping(name, expected):
    assert map_event_status_pl(name) == expected


def test_event_text_matches_regardless_of_unicode_form():
    assert map_event_status_pl("W doręczeniu") == ParcelStatus.OUT_FOR_DELIVERY


def test_normalizer_full_parcel():
    raw = delivered_sample()
    parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO, include_history=True)

    # The response's canonical number wins over the short form typed in.
    assert parcel["barcode"] == LONG_NO
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-08-06T08:24:05.343400+02:00"
    assert parcel["raw_status"] == "Paczka doręczona"
    assert parcel["pickup"] is False
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["url"] == (
        f"https://gls-group.com/PL/en/parcel-tracking/?match={LONG_NO}"
    )
    # History is republished oldest-first, whatever the wire order was.
    assert [event["status"] for event in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert parcel["history"][0]["raw_status"] == "Utworzenie paczki"
    assert parcel["raw"] is raw


def test_barcode_falls_back_to_the_entered_code():
    parcel = normalize_parcel_pl({"progressBarIdent": "INTRANSIT"}, parcel_no=SHORT_NO)
    assert parcel["barcode"] == SHORT_NO


def test_an_unknown_shipment_code_is_derived_from_the_newest_event(caplog):
    raw = {
        "progressBarIdent": "SOMETHINGNEW",
        "eventReasons": [
            _event("W doręczeniu", "Paczka w doręczeniu ", "2026-08-06T06:33:12+02:00")
        ],
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert sum("Unrecognised GLS Poland status" in r.message for r in caplog.records) == 1


def test_an_unknown_event_text_stays_neutral_and_warns_once(caplog):
    raw = {
        "progressBarIdent": "INTRANSIT",
        "eventReasons": [
            _event("Zupełnie nowy status", "Opis", "2026-08-06T06:33:12+02:00"),
            _event("Zupełnie nowy status", "Opis", "2026-08-05T06:33:12+02:00"),
        ],
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO, include_history=True)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert [event["status"] for event in parcel["history"]] == [None, None]
    assert sum("Unrecognised GLS Poland event" in r.message for r in caplog.records) == 1


def test_a_shop_dropoff_reported_as_delivered_warns_once(caplog):
    """The one open PL question: what the shipment code says while a parcel waits."""
    raw = {
        "progressBarIdent": "DELIVERED",
        "stateDate": "2026-08-11T12:51:25+02:00",
        "eventReasons": [
            _event(
                "Gotowa do odbioru",
                "Paczka doręczona - do Punktu GLS",
                "2026-08-11T12:51:25+02:00",
                "PL6200",
            )
        ],
    }
    with caplog.at_level(logging.WARNING):
        normalize_parcel_pl(raw, parcel_no=SHORT_NO)
        normalize_parcel_pl(raw, parcel_no=SHORT_NO)
    assert sum("GLS Point drop-off" in r.message for r in caplog.records) == 1


def test_unexpected_top_level_keys_are_reported_by_name_and_type_once(caplog):
    raw = {"progressBarIdent": "DELIVERED", "recipientName": "Jan Kowalski"}
    with caplog.at_level(logging.WARNING):
        normalize_parcel_pl(raw, parcel_no=SHORT_NO)
        normalize_parcel_pl(raw, parcel_no=SHORT_NO)
    messages = [
        r.getMessage() for r in caplog.records if "unexpected top-level" in r.message
    ]
    assert len(messages) == 1
    assert "recipientName: str" in messages[0]
    # Names and types only — never a value.
    assert "Kowalski" not in messages[0]


def test_unparseable_timestamps_are_dropped_and_warned_once(caplog):
    raw = {
        "progressBarIdent": "DELIVERED",
        "stateDate": "06.08.2026",
        "eventReasons": [
            _event("Doręczona", "Paczka doręczona", "06.08.2026"),
            _event("W drodze", "Paczka w drodze", "2026-08-06T06:31:39+02:00"),
        ],
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO, include_history=True)
    assert [event["timestamp"] for event in parcel["history"]] == [
        "2026-08-06T06:31:39+02:00"
    ]
    # No usable stateDate, so the newest parseable event stands in.
    assert parcel["delivered_at"] == "2026-08-06T06:31:39+02:00"
    assert sum("unparseable timestamp" in r.message for r in caplog.records) == 1


def test_history_not_newest_first_is_reported_once(caplog):
    raw = {
        "progressBarIdent": "INTRANSIT",
        "eventReasons": [
            _event("Utworzenie paczki", "Nadanie", "2026-08-04T08:56:34+02:00"),
            _event("W drodze", "W drodze", "2026-08-06T06:31:39+02:00"),
        ],
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_pl(raw, parcel_no=SHORT_NO, include_history=True)
        normalize_parcel_pl(raw, parcel_no=SHORT_NO, include_history=True)
    # Sorted regardless of what the carrier sent.
    assert [event["raw_status"] for event in parcel["history"]] == [
        "Utworzenie paczki",
        "W drodze",
    ]
    assert sum("not newest-first" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_blank_statuses_are_neutral_without_warning(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_pl(value) == ParcelStatus.UNKNOWN
        assert map_event_status_pl(value) is None
    assert not caplog.records


async def test_transport_requests_the_number_alone():
    body = delivered_sample()
    session = _session(200, body)

    assert await async_get_parcel_pl(session, SHORT_NO, "00-001") is body
    url = session.get.call_args.args[0]
    assert url == (
        "https://mygls.gls-poland.com.pl/api/v1/mygls-tracking/public/tracking/"
        f"shipment/track/{SHORT_NO}"
    )
    assert "00-001" not in str(session.get.call_args)


async def test_transport_treats_the_carriers_400_as_not_found():
    session = _session(
        400,
        {
            "code": "mygls-tracking-400(111)",
            "errorId": "5VijyP",
            "message": "Żądanie śledzenia paczki nie powiodło się.",
        },
    )
    assert await async_get_parcel_pl(session, SHORT_NO, "00-001") is None


async def test_transport_raises_when_the_public_prefix_is_closed():
    session = _session(401, {"message": "Unauthorized"})
    with pytest.raises(GlsApiError):
        await async_get_parcel_pl(session, SHORT_NO, "00-001")


async def test_transport_raises_on_an_unexpected_400():
    """A 400 without the carrier's own code is not a missing parcel."""
    session = _session(400, {"code": "mygls-tracking-400(999)"})
    with pytest.raises(GlsApiError):
        await async_get_parcel_pl(session, SHORT_NO, "00-001")


async def test_transport_returns_none_for_a_non_json_body(caplog):
    session = _session(200, None)
    response = await session.get.return_value.__aenter__()
    response.json = AsyncMock(side_effect=ValueError("not json"))

    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_pl(session, SHORT_NO, "00-001") is None
    assert any("non-JSON body" in r.message for r in caplog.records)


async def test_transport_returns_none_for_a_non_object_body(caplog):
    session = _session(200, ["not", "an", "object"])
    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_pl(session, SHORT_NO, "00-001") is None
    assert any("unexpected top-level type" in r.message for r in caplog.records)
