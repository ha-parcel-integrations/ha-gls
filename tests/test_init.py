"""Tests for GLS setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.const import (
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DOMAIN,
)

_SAMPLE = {
    "parcelNo": "0085105093278",
    "state": 3,
    "addressInfo": {"from": {"name": "Sender"}, "to": {"name": "R"}},
    "deliveryScanInfo": {"isDelivered": False, "dateTime": None},
    "deliveryStatus": {"etaTimestampMin": "2026-05-01T10:00:00Z", "etaTimestampMax": None},
    "parcels": [{"lastStatus": "Onderweg"}],
    "scans": [{"dateTime": "2026-04-30T10:00:00", "state": 1, "eventReasonDescr": "x"}],
}


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.gls_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_SAMPLE)
    with patch("custom_components.gls.api.GlsApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_0085105093278"
        )

        # The next poll returns a different parcel number: the summary sensor
        # spawns a new per-parcel sensor and removes the stale one.
        replaced = dict(_SAMPLE)
        replaced["parcelNo"] = "2222222222222"
        mock.return_value = replaced
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_2222222222222"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_0085105093278"
            )
            is None
        )


async def test_legacy_unique_id_migrates_to_postcode(hass):
    """Pre-multi-hub entries (unique_id == DOMAIN) migrate to their postcode,
    so the flow's per-postcode duplicate guard also covers them."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: "1234AB"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == "1234AB"

    # A second hub for the same postcode now aborts instead of duplicating.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
