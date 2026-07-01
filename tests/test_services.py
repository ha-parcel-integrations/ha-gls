"""Tests for the GLS services (track_parcel / untrack_parcel)."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
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
    "deliveryStatus": {"etaTimestampMin": None, "etaTimestampMax": None},
    "parcels": [{"lastStatus": "Onderweg"}],
    "scans": [],
}


async def _setup(hass) -> MockConfigEntry:
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
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_PARCEL_NO: "9999999999999"}, blocking=True
        )
        await hass.async_block_till_done()

    parcels = entry.options[CONF_PARCELS]
    assert parcels == [{CONF_PARCEL_NO: "9999999999999", CONF_POSTAL_CODE: "1234AB"}]


async def test_track_parcel_rejects_invalid_number(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_PARCEL_NO: "abc"}, blocking=True
        )


async def test_track_parcel_rejects_invalid_postcode(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_PARCEL_NO: "9999999999999", CONF_POSTAL_CODE: "nope"},
            blocking=True,
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN, "track_parcel", {CONF_PARCEL_NO: "9999999999999"}, blocking=True
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_untrack_parcel_removes_from_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [{CONF_PARCEL_NO: "9999999999999", CONF_POSTAL_CODE: "1234AB"}],
            CONF_POSTAL_CODE: "1234AB",
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN, "untrack_parcel", {CONF_PARCEL_NO: "9999999999999"}, blocking=True
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []
