"""Tests for the GLS services (track_parcel / untrack_parcel)."""

from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.gls.services import async_setup_services

from .payloads import minimal_no_eta_sample


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: "1234AB"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "9999999999999"}, blocking=True
        )
        await hass.async_block_till_done()

    parcels = entry.options[CONF_PARCELS]
    assert parcels == [{CONF_PARCEL_NO: "9999999999999"}]


async def test_track_parcel_rejects_invalid_postcode(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "9999999999999", CONF_POSTAL_CODE: "nope"},
            blocking=True,
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN,
                "track_parcel",
                {CONF_TRACKING_CODE: "9999999999999"},
                blocking=True,
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def _setup_hub(hass, postcode: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=postcode,
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: postcode},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_routes_to_hub_by_postcode(hass):
    home = await _setup_hub(hass, "1000AA")
    work = await _setup_hub(hass, "2000BB")

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "9999999999999", CONF_POSTAL_CODE: "2000BB"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert home.options[CONF_PARCELS] == []
    assert work.options[CONF_PARCELS] == [{CONF_PARCEL_NO: "9999999999999"}]


async def test_track_parcel_ambiguous_without_postcode(hass):
    await _setup_hub(hass, "1000AA")
    await _setup_hub(hass, "2000BB")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: "9999999999999"}, blocking=True
        )


async def test_untrack_parcel_removes_from_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "9999999999999", CONF_POSTAL_CODE: "1234AB"}
            ],
            CONF_POSTAL_CODE: "1234AB",
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_no_eta_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "9999999999999"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []


async def test_track_parcel_requires_a_code(hass):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "track_parcel", {}, blocking=True)


async def test_account_only_services_refuse_manual_changes(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-user",
        data={"source": "dk_account", "uid": "example-user"},
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    async_setup_services(hass)
    for service in ["track_parcel", "untrack_parcel"]:
        with pytest.raises(ServiceValidationError, match="managed automatically"):
            await hass.services.async_call(
                DOMAIN, service, {"tracking_code": "EXAMPLE111111"}, blocking=True
            )
    assert CONF_PARCELS not in entry.options


async def test_manual_service_ignores_account_entry(hass):
    account = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-user",
        data={"source": "dk_account"},
        options={CONF_COUNTRY: "DK", CONF_PARCELS: [{CONF_PARCEL_NO: "EXAMPLE111111"}]},
    )
    manual = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:1234AB",
        options={CONF_COUNTRY: "NL", CONF_POSTAL_CODE: "1234AB", CONF_PARCELS: []},
    )
    account.add_to_hass(hass)
    manual.add_to_hass(hass)
    async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN, "track_parcel", {"tracking_code": "EXAMPLE111111"}, blocking=True
    )
    assert manual.options[CONF_PARCELS] == [{CONF_PARCEL_NO: "EXAMPLE111111"}]
    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {"tracking_code": "EXAMPLE111111"}, blocking=True
    )
    assert manual.options[CONF_PARCELS] == []
    assert account.options[CONF_PARCELS] == [{CONF_PARCEL_NO: "EXAMPLE111111"}]


async def test_manual_services_report_missing_setup(hass):
    async_setup_services(hass)
    for service in ["track_parcel", "untrack_parcel"]:
        with pytest.raises(ServiceValidationError, match="not set up"):
            await hass.services.async_call(
                DOMAIN, service, {"tracking_code": "EXAMPLE111111"}, blocking=True
            )


async def test_track_service_rejects_empty_code_and_invalid_hub_postcode(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:invalid",
        options={CONF_POSTAL_CODE: "invalid", CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError, match="not a valid parcel number"):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": " "}, blocking=True
        )
    with pytest.raises(ServiceValidationError, match="not a valid postal code"):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_code": "EXAMPLE111111"}, blocking=True
        )
    assert entry.options[CONF_PARCELS] == []
