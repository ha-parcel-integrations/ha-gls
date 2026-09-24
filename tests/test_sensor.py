"""Tests for GLS sensor property logic."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.gls.const import DOMAIN, ParcelStatus
from custom_components.gls.sensor import (
    GlsAwaitingPickupSensor,
    GlsDeliveredParcelsSensor,
    GlsEnRouteToPickupPointSensor,
    GlsIncomingParcelsSensor,
    GlsLastUpdateSensor,
    GlsNextDeliverySensor,
    GlsParcelSensor,
    _migrate_summary_unique_ids,
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
    en_route = GlsEnRouteToPickupPointSensor(coordinator, _entry())
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


# ---------------------------------------------------------------------------
# Canonical pickup-summary unique-ID migration
# ---------------------------------------------------------------------------

_SCOPE = "e1"
_RENAMES = [
    ("en_route_to_parcel_shop", "en_route_to_pickup_point"),
]


@pytest.mark.parametrize(("old_suffix", "new_suffix"), _RENAMES)
async def test_migration_keeps_custom_entity_id(hass, old_suffix, new_suffix):
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}")
    registry.async_update_entity(old.entity_id, new_entity_id="sensor.my_pickup_parcels")

    _migrate_summary_unique_ids(registry, _SCOPE)

    new_id = registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{new_suffix}")
    assert new_id == "sensor.my_pickup_parcels"
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}") is None


@pytest.mark.parametrize(("old_suffix", "new_suffix"), _RENAMES)
async def test_migration_is_idempotent(hass, old_suffix, new_suffix):
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}")

    _migrate_summary_unique_ids(registry, _SCOPE)
    _migrate_summary_unique_ids(registry, _SCOPE)

    new_id = registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{new_suffix}")
    assert new_id == old.entity_id
    assert len(registry.entities) == 1


@pytest.mark.parametrize(("old_suffix", "new_suffix"), _RENAMES)
async def test_migration_collision_keeps_both_and_warns(
    hass, caplog, old_suffix, new_suffix
):
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}")
    new = registry.async_get_or_create("sensor", DOMAIN, f"{_SCOPE}_{new_suffix}")

    _migrate_summary_unique_ids(registry, _SCOPE)

    old_id = registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}")
    new_id = registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{new_suffix}")
    assert old_id == old.entity_id
    assert new_id == new.entity_id
    assert "reconcile" in caplog.text
