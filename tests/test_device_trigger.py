"""Tests for GLS device triggers."""
from custom_components.gls.const import DOMAIN
from custom_components.gls.device_trigger import (
    TRIGGER_EVENTS,
    async_get_triggers,
)


async def test_get_triggers_returns_all_four(hass):
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"
