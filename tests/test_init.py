"""Tests for GLS setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.api import GlsApiError
from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DE_PARCEL_NUMBER,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DOMAIN,
)

from .payloads import minimal_sample


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_sample()),
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


async def test_setup_retries_when_first_refresh_fails(hass):
    """When the first data fetch fails, setup retries from the entry itself.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(side_effect=GlsApiError("GLS unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=minimal_sample())
    with patch("custom_components.gls.api.GlsApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_0085105093278"
        )

        # The next poll returns a different parcel number: the summary sensor
        # spawns a new per-parcel sensor and removes the stale one.
        replaced = minimal_sample()
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
        new=AsyncMock(return_value=minimal_sample()),
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


async def test_de_setup_wires_country_and_de_session_end_to_end(hass):
    """The full country="DE" wiring (__init__.py -> api.py -> countries/de),
    not just the isolated unit-level mocks in test_coordinator_de.py/
    test_api.py — proves GlsApiClient/GlsCoordinator are actually
    constructed with a live GlsDeSession for a DE hub.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={CONF_DE_APP_INSTANCE_ID: "existing-app-instance-id"},
        options={
            CONF_COUNTRY: "DE",
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "075624238061", CONF_POSTAL_CODE: "00000"}
            ],
            # Keep-most-recent-100 so the delivered-retention filter's
            # default 7-day window can't trim the fixed-date sample below,
            # independent of when this test actually runs.
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)

    de_payload = {
        "trackingReference": "075624238061",
        "parcelNumber": "YOXVB8CE",
        "deliveredAt": "2026-05-31 15:39:01",
        "latestStatusText": "",
        "hasDeliveryAttemptFailed": False,
        "deliveryEvents": [],
    }
    with patch(
        "custom_components.gls.api.async_get_parcel_de",
        new=AsyncMock(return_value=de_payload),
    ) as mock_transport:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The DE transport was actually reached, with a real GlsDeSession (not
    # None) built from the persisted appInstanceId.
    mock_transport.assert_awaited_once()
    de_session_arg = mock_transport.call_args.args[1]
    assert de_session_arg.app_instance_id == "existing-app-instance-id"

    incoming = hass.states.get("sensor.gls_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "0"  # delivered, not active
    delivered = hass.states.get("sensor.gls_delivered_parcels")
    assert delivered is not None
    assert delivered.state == "1"

    # The learned parcelNumber was persisted (the cold-restart gap fix).
    assert entry.options[CONF_PARCELS][0][CONF_DE_PARCEL_NUMBER] == "YOXVB8CE"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_cz_setup_wires_group_locale_end_to_end(hass):
    """The full country="CZ" wiring (__init__.py -> api.py -> countries/cz)
    — proves ``group_locale`` (not ``culture``, which CZ's COUNTRIES row
    doesn't define) actually reaches the transport, and that barcode comes
    from the tracked parcel_no rather than the response's own tuNo.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="25401",
        options={
            CONF_COUNTRY: "CZ",
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "5036234901", CONF_POSTAL_CODE: "25401"}
            ],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)

    cz_payload = {
        "tuNo": "5036234901",
        "referenceNo": "5036234901",
        "date": "2026-06-24",
        "progressBar": {
            "statusInfo": "INTRANSIT",
            "statusText": "In transit",
            "retourFlag": False,
        },
    }
    with patch(
        "custom_components.gls.api.async_get_parcel_cz",
        new=AsyncMock(return_value=cz_payload),
    ) as mock_transport:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    mock_transport.assert_awaited_once_with(
        mock_transport.call_args.args[0],
        "gls-group.com",
        "CZ/en",
        "5036234901",
        "25401",
    )

    incoming = hass.states.get("sensor.gls_incoming_parcels")
    assert incoming is not None
