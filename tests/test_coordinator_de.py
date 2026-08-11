"""Tests for GlsCoordinator's DE-specific wiring: token-refresh failures,
parcelNumber persistence (the cold-restart gap) and reregister recovery.

NL behaviour is covered by test_coordinator.py and is untouched by any of
this — every branch here is gated on ``country == "DE"`` and a non-``None``
``de_session``.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DE_PARCEL_NUMBER,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DOMAIN,
)
from custom_components.gls.coordinator import GlsCoordinator
from custom_components.gls.countries.de import _known_parcel_numbers
from custom_components.gls.countries.de.session import GlsDeSessionError


@pytest.fixture(autouse=True)
def _reset_transport_cache():
    """The DE transport's parcelNumber cache is process-global — isolate tests."""
    _known_parcel_numbers.clear()
    yield
    _known_parcel_numbers.clear()


def _de_entry(parcels: list[dict], *, app_instance_id: str = "existing-id") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        data={CONF_DE_APP_INSTANCE_ID: app_instance_id},
        options={CONF_COUNTRY: "DE", CONF_PARCELS: parcels},
    )


def _de_session(app_instance_id: str = "existing-id", *, reregistered: bool = False):
    session = MagicMock()
    session.app_instance_id = app_instance_id
    session.pop_reregistered = MagicMock(return_value=reregistered)
    return session


async def test_de_session_error_raises_update_failed(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _de_entry([{CONF_PARCEL_NO: "075624238061", CONF_POSTAL_CODE: "00000"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = GlsDeSessionError("token refresh failed")
    coordinator = GlsCoordinator(hass, client, entry, de_session=_de_session())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_parcel_number_persisted_after_successful_fetch(hass):
    entry = _de_entry([{CONF_PARCEL_NO: "075624238061", CONF_POSTAL_CODE: "00000"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = {
        "trackingReference": "075624238061",
        "parcelNumber": "YOXVB8CE",
        "deliveredAt": "2026-05-31 15:39:01",
        "latestStatusText": "",
        "hasDeliveryAttemptFailed": False,
        "deliveryEvents": [],
    }
    coordinator = GlsCoordinator(hass, client, entry, de_session=_de_session())

    await coordinator._async_update_data()

    stored = entry.options[CONF_PARCELS][0]
    assert stored[CONF_DE_PARCEL_NUMBER] == "YOXVB8CE"


async def test_persisted_parcel_number_seeds_transport_cache_before_poll(hass):
    entry = _de_entry(
        [
            {
                CONF_PARCEL_NO: "075624238061",
                CONF_POSTAL_CODE: "00000",
                CONF_DE_PARCEL_NUMBER: "YOXVB8CE",
            }
        ]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = {
        "trackingReference": "075624238061",
        "parcelNumber": "YOXVB8CE",
        "deliveredAt": None,
        "latestStatusText": "",
        "hasDeliveryAttemptFailed": False,
        "deliveryEvents": [],
    }
    coordinator = GlsCoordinator(hass, client, entry, de_session=_de_session())

    assert "075624238061" not in _known_parcel_numbers
    await coordinator._async_update_data()
    assert _known_parcel_numbers["075624238061"] == "YOXVB8CE"


async def test_reregister_clears_parcel_number_and_persists_new_app_instance_id(hass):
    entry = _de_entry(
        [
            {
                CONF_PARCEL_NO: "075624238061",
                CONF_POSTAL_CODE: "00000",
                CONF_DE_PARCEL_NUMBER: "YOXVB8CE",
            }
        ],
        app_instance_id="old-id",
    )
    entry.add_to_hass(hass)
    _known_parcel_numbers["075624238061"] = "YOXVB8CE"
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # e.g. re-add in progress
    de_session = _de_session(app_instance_id="new-id", reregistered=True)
    coordinator = GlsCoordinator(hass, client, entry, de_session=de_session)

    await coordinator._async_update_data()

    assert entry.data[CONF_DE_APP_INSTANCE_ID] == "new-id"
    assert CONF_DE_PARCEL_NUMBER not in entry.options[CONF_PARCELS][0]
    assert "075624238061" not in _known_parcel_numbers


async def test_nl_coordinator_never_touches_de_helpers(hass):
    """No de_session at all — the DE branches must never run for NL."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234AB",
        options={
            CONF_PARCELS: [{CONF_PARCEL_NO: "123", CONF_POSTAL_CODE: "1234AB"}]
        },
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = {"parcelNo": "123", "state": 4}
    coordinator = GlsCoordinator(hass, client, entry)  # de_session defaults to None

    # Must not raise despite no de_session being set.
    await coordinator._async_update_data()
    assert "123" not in _known_parcel_numbers
