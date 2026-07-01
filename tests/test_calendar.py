"""Tests for the GLS deliveries calendar."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.gls.calendar import GlsDeliveriesCalendar


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _parcel(barcode, planned_from=None, planned_to=None, pickup=False, pickup_point=None):
    return {
        "barcode": barcode,
        "sender": "Example Sender",
        "status": "out_for_delivery",
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": "https://track/1",
    }


def _cal(data):
    return GlsDeliveriesCalendar(_coordinator(data), _entry())


def test_event_returns_earliest_upcoming():
    cal = _cal([
        _parcel("LATE", planned_from="2099-01-02T10:00:00Z"),
        _parcel("SOON", planned_from="2099-01-01T10:00:00Z"),
    ])
    event = cal.event
    assert event.uid == "SOON"
    assert event.summary == "Example Sender"


def test_event_none_when_no_planned():
    assert _cal([_parcel("A")]).event is None


def test_moment_gets_one_hour_duration():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert events[0].end == datetime(2099, 1, 1, 11, 0, tzinfo=timezone.utc)


def test_interval_uses_window():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z", planned_to="2099-01-01T12:00:00Z")])
    assert cal._events()[0].end == datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_pickup_sets_location():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z", pickup=True, pickup_point="GLS Shop")])
    assert cal._events()[0].location == "GLS Shop"


def test_naive_planned_from_treated_as_utc():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00")])  # no tz
    assert cal._events()[0].start == datetime(2099, 1, 1, 10, 0, tzinfo=timezone.utc)


def test_unparseable_planned_from_skipped():
    assert _cal([_parcel("A", planned_from="not-a-date")]).event is None


async def test_get_events_filters_by_range():
    cal = _cal([
        _parcel("PAST", planned_from="2000-01-01T10:00:00Z"),
        _parcel("FUTURE", planned_from="2099-01-01T10:00:00Z"),
    ])
    start = datetime(2098, 1, 1, tzinfo=timezone.utc)
    end = datetime(2100, 1, 1, tzinfo=timezone.utc)
    events = await cal.async_get_events(MagicMock(), start, end)
    assert {e.uid for e in events} == {"FUTURE"}
