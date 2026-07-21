"""Sensor platform for the GLS parcel tracker integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GlsConfigEntry
from .const import CONF_POSTAL_CODE, DOMAIN, ParcelStatus
from .coordinator import GlsCoordinator
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out to all entities; HA's per-entity
# update throttling adds nothing here.
PARALLEL_UPDATES = 0




async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GLS sensor entities from a config entry."""
    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = entry.runtime_data.coordinator

    current_barcodes: set[str] = {
        p.get("barcode", "") for p in coordinator.data or []
    }
    entry_id = entry.entry_id

    # Remove per-parcel sensors from the registry whose barcode is no longer
    # active (e.g. the code was removed, or the parcel was delivered between
    # restarts). Scoped to the sensor domain so it never touches the refresh
    # button or the diagnostic last-update sensor.
    registry = er.async_get(hass)
    non_parcel_unique_ids = {
        f"{entry_id}_incoming_parcels",
        f"{entry_id}_next_delivery",
        f"{entry_id}_en_route_to_parcel_shop",
        f"{entry_id}_awaiting_pickup",
        f"{entry_id}_delivered_parcels",
        f"{entry_id}_last_update",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry_id):
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{entry_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            barcode = entity_entry.unique_id[len(f"{entry_id}_"):]
            if barcode not in current_barcodes:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        GlsIncomingParcelsSensor(coordinator, entry, async_add_entities, current_barcodes),
    ]
    for parcel in coordinator.data or []:
        entities.append(
            GlsParcelSensor(coordinator, entry, parcel.get("barcode", ""))
        )
    entities.append(GlsNextDeliverySensor(coordinator, entry))
    entities.append(GlsEnRouteToParcelShopSensor(coordinator, entry))
    entities.append(GlsAwaitingPickupSensor(coordinator, entry))
    entities.append(GlsDeliveredParcelsSensor(coordinator, entry))
    entities.append(GlsLastUpdateSensor(coordinator, entry))

    async_add_entities(entities)


class GlsIncomingParcelsSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Summary sensor: count of active (not-yet-delivered) tracked parcels.

    Spawns a per-parcel sensor for each new barcode and removes stale ones
    from the registry (via the registry, not self-removal, to avoid the ghost
    entity race).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by GLS"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: GlsCoordinator,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
        known_barcodes: set[str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._attr_unique_id = f"{entry.entry_id}_incoming_parcels"
        self._attr_device_info = build_device_info(entry)
        self._known_barcodes: set[str] = known_barcodes or set()

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self.coordinator.data or []}

    def _handle_coordinator_update(self) -> None:
        current_barcodes: set[str] = {
            p.get("barcode", "") for p in (self.coordinator.data or [])
        }

        new_barcodes = current_barcodes - self._known_barcodes
        if new_barcodes:
            self._async_add_entities(
                GlsParcelSensor(self.coordinator, self._entry, barcode)
                for barcode in new_barcodes
            )

        removed_barcodes = self._known_barcodes - current_barcodes
        if removed_barcodes:
            registry = er.async_get(self.hass)
            for barcode in removed_barcodes:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self._entry.entry_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_barcodes = current_barcodes
        super()._handle_coordinator_update()


class GlsParcelSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Per-parcel sensor reporting the status of a single tracked GLS parcel."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcel"
    _attr_attribution = "Data provided by GLS"
    _unrecorded_attributes = frozenset({"raw", "history"})

    def __init__(
        self, coordinator: GlsCoordinator, entry: ConfigEntry, barcode: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._barcode = barcode
        self._attr_unique_id = f"{entry.entry_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}
        self._attr_device_info = build_device_info(entry)

    def _get_parcel(self) -> dict[str, Any] | None:
        for parcel in self.coordinator.data or []:
            if parcel.get("barcode") == self._barcode:
                return parcel
        return None

    @property
    def native_value(self) -> str | None:
        parcel = self._get_parcel()
        return parcel.get("status") if parcel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}


class GlsNextDeliverySensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Earliest expected delivery datetime across all active parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = "Data provided by GLS"

    def __init__(self, coordinator: GlsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_delivery"
        self._attr_device_info = build_device_info(entry)

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        for parcel in self.coordinator.data or []:
            moment_str = parcel.get("planned_from")
            if not moment_str:
                continue
            try:
                dt = datetime.fromisoformat(moment_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result.append((dt, parcel))
            except ValueError:
                _LOGGER.debug("Could not parse delivery moment: %s", moment_str)
        return result

    @property
    def native_value(self) -> datetime | None:
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
            "receiver": earliest.get("receiver"),
        }


class GlsEnRouteToParcelShopSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Active parcels still in transit to a GLS ParcelShop."""

    _attr_has_entity_name = True
    _attr_translation_key = "en_route_to_parcel_shop"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by GLS"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: GlsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_en_route_to_parcel_shop"
        self._attr_device_info = build_device_info(entry)

    def _parcels(self) -> list[dict]:
        return [
            p for p in (self.coordinator.data or [])
            if p.get("pickup") and p.get("status") != ParcelStatus.AT_PICKUP_POINT
        ]

    @property
    def native_value(self) -> int:
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self._parcels()}


class GlsAwaitingPickupSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Parcels that have arrived at a GLS ParcelShop and are ready to collect."""

    _attr_has_entity_name = True
    _attr_translation_key = "awaiting_pickup"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by GLS"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: GlsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_awaiting_pickup"
        self._attr_device_info = build_device_info(entry)

    def _parcels(self) -> list[dict]:
        return [
            p for p in (self.coordinator.data or [])
            if p.get("pickup") and p.get("status") == ParcelStatus.AT_PICKUP_POINT
        ]

    @property
    def native_value(self) -> int:
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self._parcels()}


class GlsDeliveredParcelsSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Recently delivered tracked GLS parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by GLS"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: GlsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_delivered_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.delivered)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self.coordinator.delivered}


class GlsLastUpdateSensor(CoordinatorEntity[GlsCoordinator], SensorEntity):
    """Diagnostic sensor reporting when GLS was last polled successfully."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = "Data provided by GLS"

    def __init__(self, coordinator: GlsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_update"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_success_time
