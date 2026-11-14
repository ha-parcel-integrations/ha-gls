"""Danish account parcel normalization with synthetic parcel data."""

from copy import deepcopy
import logging

import pytest

from custom_components.gls.const import HISTORY_MAX_EVENTS, ParcelStatus
from custom_components.gls.countries import dk
from custom_components.gls.countries.dk import map_event_status_dk, normalize_parcel_dk


@pytest.fixture(autouse=True)
def _reset_warnings():
    dk._unmapped_states_logged.clear()
    yield
    dk._unmapped_states_logged.clear()


def _event(state, reason=0, when="2026-10-01T10:00:00Z", text="Synthetic event"):
    return {
        "parcelState": state,
        "eventReasonCombination": reason,
        "eventDateTime": when,
        "parcelStateText": text,
        "isFinal": False,
        "location": "Example depot",
    }


def _parcel(events=None, **values):
    return {
        "parcelNumber": "999000111222",
        "senderName": "Example Sender",
        "receiverName": "Example Recipient",
        "receiverAddress": "Example Street 1",
        "receiverZipCode": "9999",
        "receiverCity": "Example City",
        "actualWeight": 3.0,
        "parcelShopNumber": "123",
        "scheduledReturnDate": "2026-10-15T12:00:00Z",
        "isDelivered": False,
        "parcelStatusDtoList": events if events is not None else [_event(35, 5)],
        **values,
    }


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        (10, 1444, ParcelStatus.REGISTERED),
        (30, 16, ParcelStatus.IN_TRANSIT),
        (35, 5, ParcelStatus.IN_TRANSIT),
        (40, 221, ParcelStatus.OUT_FOR_DELIVERY),
        (40, 1586, ParcelStatus.IN_TRANSIT),
        (50, 1573, ParcelStatus.DELIVERED),
        (50, 46, ParcelStatus.DELIVERED),
        (50, 48, ParcelStatus.DELIVERED),
        (60, 54, ParcelStatus.AT_PICKUP_POINT),
        (70, 1634, ParcelStatus.DELIVERED),
        (80, 1579, ParcelStatus.PROBLEM),
        (100, 132, ParcelStatus.RETURNING),
    ],
)
def test_observed_states_map(state, reason, expected):
    assert map_event_status_dk(_event(state, reason)) == expected


def test_normalizer_keeps_contract_and_does_not_invent_delivery_window():
    raw = _parcel()
    parcel = normalize_parcel_dk(raw)
    assert parcel["barcode"] == raw["parcelNumber"]
    assert parcel["sender"] == "Example Sender"
    assert parcel["receiver"] == "Example Recipient"
    assert parcel["weight"] == 3.0
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Synthetic event"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["delivered_at"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None
    assert parcel["dimensions"] is None
    assert "999000111222" in parcel["url"]
    assert parcel["raw"] is not raw


def test_status_and_history_use_timestamps_not_array_positions():
    raw = _parcel(
        [
            _event(10, when="2026-10-01T09:00:00Z"),
            _event(50, when="2026-10-01T12:00:00+02:00"),
            _event(35, when="2026-10-01T09:30:00Z"),
        ]
    )
    parcel = normalize_parcel_dk(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-10-01T12:00:00+02:00"
    assert [event["status"] for event in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.DELIVERED,
    ]


def test_latest_problem_is_not_replaced_by_old_delivered_event():
    parcel = normalize_parcel_dk(
        _parcel(
            [
                _event(50, when="2026-10-01T09:00:00Z"),
                _event(80, when="2026-10-01T10:00:00Z"),
            ]
        )
    )
    assert parcel["status"] == ParcelStatus.PROBLEM
    assert parcel["delivered"] is False


def test_unknown_latest_falls_back_to_latest_known_event_and_warns_once(caplog):
    raw = _parcel(
        [
            _event(999, 999, when="2026-10-01T11:00:00Z"),
            _event(35, when="2026-10-01T09:00:00Z"),
        ]
    )
    with caplog.at_level(logging.WARNING):
        first = normalize_parcel_dk(raw, include_history=True)
        normalize_parcel_dk(raw)
    assert first["status"] == ParcelStatus.IN_TRANSIT
    assert first["history"][-1]["status"] is None
    assert len(caplog.records) == 1
    assert "state=999 reason=999" in caplog.text
    assert "Synthetic event" not in caplog.text
    assert "issues/new" in caplog.text


@pytest.mark.parametrize(
    "event",
    [
        _event(40, 999),
        _event(999, 1),
        _event("private-value", "private-reason"),
        _event(True, False),
        {},
    ],
)
def test_unmapped_codes_are_neutral_and_log_no_arbitrary_values(event, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_event_status_dk(event) is None
    assert "private-value" not in caplog.text
    assert "private-reason" not in caplog.text


@pytest.mark.parametrize(("state", "reason"), [(60, 54), (40, 1586), (70, 1634)])
def test_pickup_evidence_survives_collection_history(state, reason):
    events = [_event(state, reason)]
    if state == 70:
        events.append(_event(60, 54, when="2026-10-01T08:00:00Z"))
    parcel = normalize_parcel_dk(_parcel(events))
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Street 1, 9999, Example City"


def test_pickup_falls_back_to_shop_number_without_address():
    parcel = normalize_parcel_dk(
        _parcel(
            [_event(60)], receiverAddress=None, receiverZipCode=None, receiverCity=None
        )
    )
    assert parcel["pickup_point"] == "123"


def test_delivered_flag_is_authoritative_but_final_flag_is_not():
    raw = _parcel([_event(35)], isDelivered=True)
    assert normalize_parcel_dk(raw)["delivered"] is True
    raw["isDelivered"] = False
    raw["parcelStatusDtoList"][0]["isFinal"] = True
    assert normalize_parcel_dk(raw)["delivered"] is False


def test_access_material_and_unreviewed_nested_fields_never_leave_normalizer():
    raw = _parcel(
        pickUpCode="private-pickup",
        refreshToken="private-refresh",
        idToken="private-id",
        token="private-token",
        shareParcelLink="https://example.invalid/?token=private-link",
        podImageUrl="https://example.invalid/?sig=private-signature",
        extension={"pin": "private-nested"},
    )
    raw["parcelStatusDtoList"][0].update(
        {"pickUpCode": "private-history", "extension": {"token": "private-event-token"}}
    )
    original = deepcopy(raw)
    parcel = normalize_parcel_dk(raw, include_history=True)
    assert "private-" not in str(parcel)
    assert raw == original
    assert "pickUpCode" not in parcel["raw"]


def test_nested_values_cannot_smuggle_access_material_in_known_fields():
    raw = _parcel(receiverName={"token": "private"}, senderName=[{"pin": "private"}])
    raw["parcelStatusDtoList"][0]["location"] = {"token": "private"}
    assert "private" not in str(normalize_parcel_dk(raw))


@pytest.mark.parametrize("events", [None, "unexpected", [None, 123], []])
def test_missing_event_list_is_unknown(events):
    raw = _parcel()
    raw["parcelStatusDtoList"] = events
    parcel = normalize_parcel_dk(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["history"] == []


def test_malformed_times_do_not_break_history_and_are_not_delivery_dates():
    raw = _parcel([_event(50, when="invalid"), _event(10, when=None)])
    parcel = normalize_parcel_dk(raw, include_history=True)
    assert parcel["delivered_at"] is None
    assert parcel["history"] == []


@pytest.mark.parametrize("weight", [None, True, "3.0", {}, -1])
def test_only_numeric_weight_is_exposed(weight):
    parcel = normalize_parcel_dk(_parcel(actualWeight=weight))
    assert parcel["weight"] is None


def test_history_is_bounded_and_optional_strings_are_type_checked():
    events = [_event(35, when=f"2026-10-01T10:{minute:02}:00Z") for minute in range(59)]
    parcel = normalize_parcel_dk(
        _parcel(events, parcelNumber=None, senderName=123, receiverName=""),
        include_history=True,
    )
    assert len(parcel["history"]) == min(59, HISTORY_MAX_EVENTS)
    assert parcel["barcode"] is None
    assert parcel["url"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_unknown_after_delivery_does_not_reaffirm_old_completion():
    parcel = normalize_parcel_dk(
        _parcel(
            [
                _event(999, when="2026-10-01T11:00:00Z"),
                _event(50, when="2026-10-01T10:00:00Z"),
            ]
        )
    )
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False


def test_history_preserves_source_order_when_timestamps_tie():
    raw = _parcel(
        [
            _event(40, 221, text="First equal-time scan"),
            _event(35, 5, text="Second equal-time scan"),
            _event(10, when="2026-10-01T08:00:00Z"),
        ]
    )
    parcel = normalize_parcel_dk(raw, include_history=True)
    assert [row["raw_status"] for row in parcel["history"]][-2:] == [
        "First equal-time scan",
        "Second equal-time scan",
    ]


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_measurements_never_enter_attributes_or_raw(weight):
    parcel = normalize_parcel_dk(_parcel(actualWeight=weight))
    assert parcel["weight"] is None
    assert "actualWeight" not in parcel["raw"]
