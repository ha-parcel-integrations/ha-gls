"""Tests for GLS sensor property logic."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from homeassistant.helpers import entity_registry as er
import pytest

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
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2099-05-02T10:00:00Z"),
            _parcel("B", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = GlsNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2099, 5, 1, 10, 0, tzinfo=UTC)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_carries_the_contents():
    parcel = _parcel("A", planned_from="2099-05-01T10:00:00Z")
    parcel["contents"] = "Running shoes"
    sensor = GlsNextDeliverySensor(_coordinator([parcel]), _entry())
    assert sensor.extra_state_attributes["contents"] == "Running shoes"


def test_next_delivery_none_without_moments():
    sensor = GlsNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_en_route_and_awaiting_pickup_split():
    coordinator = _coordinator(
        [
            _parcel("A", status=ParcelStatus.IN_TRANSIT, pickup=True),
            _parcel("B", status=ParcelStatus.AT_PICKUP_POINT, pickup=True),
            _parcel("C", status=ParcelStatus.IN_TRANSIT, pickup=False),
        ]
    )
    en_route = GlsEnRouteToPickupPointSensor(coordinator, _entry())
    awaiting = GlsAwaitingPickupSensor(coordinator, _entry())
    assert en_route.native_value == 1
    assert awaiting.native_value == 1
    assert awaiting._parcels()[0]["barcode"] == "B"


def test_delivered_sensor():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = GlsDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
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
    registry.async_update_entity(
        old.entity_id, new_entity_id="sensor.my_pickup_parcels"
    )

    _migrate_summary_unique_ids(registry, _SCOPE)

    new_id = registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{new_suffix}")
    assert new_id == "sensor.my_pickup_parcels"
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{_SCOPE}_{old_suffix}") is None
    )


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


def _in(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def test_next_delivery_skips_a_window_that_has_ended():
    stale = _parcel("A", planned_from=_in(days=-3))
    stale["planned_to"] = _in(days=-2)
    moment_only = _parcel("B", planned_from=_in(hours=-2))
    sensor = GlsNextDeliverySensor(
        _coordinator([stale, moment_only, _parcel("C", planned_from=_in(days=2))]),
        _entry(),
    )
    assert sensor.extra_state_attributes["barcode"] == "C"


def test_next_delivery_keeps_a_window_that_is_still_running():
    running = _parcel("A", planned_from=_in(hours=-1))
    running["planned_to"] = _in(hours=2)
    sensor = GlsNextDeliverySensor(_coordinator([running]), _entry())
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_en_route_leaves_out_a_returning_pickup_parcel():
    parcels = [
        _parcel("BACK", status=ParcelStatus.RETURNING, pickup=True),
        _parcel("GOING", status=ParcelStatus.IN_TRANSIT, pickup=True),
    ]
    sensor = GlsEnRouteToPickupPointSensor(_coordinator(parcels), _entry())
    assert [p["barcode"] for p in sensor.extra_state_attributes["parcels"]] == ["GOING"]
