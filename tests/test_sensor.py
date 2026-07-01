"""Tests for GLS sensor property logic."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.gls.const import ParcelStatus
from custom_components.gls.sensor import (
    GlsAwaitingPickupSensor,
    GlsDeliveredParcelsSensor,
    GlsEnRouteToParcelShopSensor,
    GlsIncomingParcelsSensor,
    GlsLastUpdateSensor,
    GlsNextDeliverySensor,
    GlsParcelSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict], delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "GLS",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": "Receiver",
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = GlsIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = GlsParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = GlsParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator([
        _parcel("A", planned_from="2026-05-02T10:00:00Z"),
        _parcel("B", planned_from="2026-05-01T10:00:00Z"),
    ])
    sensor = GlsNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_none_without_moments():
    sensor = GlsNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_en_route_and_awaiting_pickup_split():
    coordinator = _coordinator([
        _parcel("A", status=ParcelStatus.IN_TRANSIT, pickup=True),
        _parcel("B", status=ParcelStatus.AT_PICKUP_POINT, pickup=True),
        _parcel("C", status=ParcelStatus.IN_TRANSIT, pickup=False),
    ])
    en_route = GlsEnRouteToParcelShopSensor(coordinator, _entry())
    awaiting = GlsAwaitingPickupSensor(coordinator, _entry())
    assert en_route.native_value == 1
    assert awaiting.native_value == 1
    assert awaiting._parcels()[0]["barcode"] == "B"


def test_delivered_sensor():
    coordinator = _coordinator([], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)])
    sensor = GlsDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = GlsLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment
