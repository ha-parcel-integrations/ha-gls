"""Account discovery uses complete snapshots and the shared parcel lifecycle."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.account_coordinator import GlsAccountCoordinator
from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.gls.countries.dk.session import GlsDkApiError, GlsDkAuthError


def raw_parcel(number="EXAMPLE123456", *, delivered=False):
    return {
        "parcelNumber": number,
        "userId": "example-firebase-user",
        "senderName": "Example shop",
        "receiverName": "Example recipient",
        "pickUpCode": "secret-pickup-code",
        "isDelivered": delivered,
        "parcelStatusDtoList": [
            {
                "parcelState": 50 if delivered else 35,
                "eventReasonCombination": 46 if delivered else 5,
                "eventDateTime": "2026-11-10T12:00:00Z",
                "parcelStateText": "Delivered" if delivered else "In transit",
            }
        ],
    }


def account(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-firebase-user",
        data={"source": "dk_account", "uid": "example-firebase-user"},
        options={
            CONF_COUNTRY: "DK",
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)
    session = SimpleNamespace(
        uid="example-firebase-user", async_get_parcels=AsyncMock()
    )
    return GlsAccountCoordinator(hass, session, entry), session


async def test_empty_account_keeps_polling_and_discovers_new_parcel(hass):
    coordinator, session = account(hass)
    session.async_get_parcels.return_value = {"totalCount": 0, "items": []}
    with patch(
        "custom_components.gls.coordinator.dt_util.now",
        return_value=datetime(2026, 11, 10, 12, tzinfo=UTC),
    ):
        assert await coordinator._async_update_data() == []
        assert coordinator.update_interval.total_seconds() >= 45 * 60
        session.async_get_parcels.return_value = {
            "totalCount": 1,
            "items": [raw_parcel()],
        }
        data = await coordinator._async_update_data()
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT
    assert data[0]["barcode"] == "EXAMPLE123456"
    assert "pickUpCode" not in data[0]["raw"]
    assert coordinator.last_success_time is not None


async def test_delivered_account_keeps_discovery_and_removed_parcel_disappears(hass):
    coordinator, session = account(hass)
    session.async_get_parcels.return_value = {
        "totalCount": 1,
        "items": [raw_parcel(delivered=True)],
    }
    assert await coordinator._async_update_data() == []
    assert len(coordinator.delivered) == 1
    assert coordinator.current_tier_minutes == 45
    assert coordinator.update_interval is not None
    session.async_get_parcels.return_value = {"totalCount": 0, "items": []}
    assert await coordinator._async_update_data() == []
    assert coordinator.delivered == []


@pytest.mark.parametrize(
    "feed",
    [
        None,
        [],
        {},
        {"totalCount": -1, "items": []},
        {"totalCount": True, "items": []},
        {"totalCount": 2, "items": []},
        {"totalCount": 0, "items": None},
        {"totalCount": 1, "items": [None]},
        {
            "totalCount": 1,
            "items": [{"parcelNumber": "", "userId": "example-firebase-user"}],
        },
        {
            "totalCount": 1,
            "items": [{"parcelNumber": 123, "userId": "example-firebase-user"}],
        },
        {
            "totalCount": 1,
            "items": [{"parcelNumber": "OTHER", "userId": "other-account"}],
        },
        {"totalCount": 2, "items": [raw_parcel(), raw_parcel()]},
        {"totalCount": 2, "items": [raw_parcel(), raw_parcel(" EXAMPLE123456 ")]},
    ],
)
async def test_invalid_or_incomplete_feed_never_replaces_snapshot(hass, feed):
    coordinator, session = account(hass)
    coordinator.delivered = [{"barcode": "retained"}]
    coordinator._known_state = {"retained": ParcelStatus.DELIVERED}
    session.async_get_parcels.return_value = feed
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.delivered == [{"barcode": "retained"}]
    assert coordinator._known_state == {"retained": ParcelStatus.DELIVERED}
    assert coordinator.last_success_time is None
    assert coordinator.update_interval is not None


async def test_transport_error_preserves_last_success_and_data(hass):
    coordinator, session = account(hass)
    session.async_get_parcels.return_value = {"totalCount": 1, "items": [raw_parcel()]}
    await coordinator.async_refresh()
    original = coordinator.data
    last_success = coordinator.last_success_time
    session.async_get_parcels.side_effect = GlsDkApiError("request failed")
    await coordinator.async_refresh()
    assert coordinator.data == original
    assert coordinator.last_success_time == last_success
    assert coordinator.last_update_success is False


async def test_auth_failure_becomes_ha_reauth(hass):
    coordinator, session = account(hass)
    session.async_get_parcels.side_effect = GlsDkAuthError("sign-in required")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("status", [401, 403])
async def test_backend_rejection_is_retried_not_reauthenticated(hass, status):
    coordinator, session = account(hass)
    session.async_get_parcels.side_effect = GlsDkApiError(
        "rejected", status_code=status
    )
    with pytest.raises(UpdateFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.retry_after is None


async def test_account_feed_is_read_whole_so_nothing_is_skipped_from_the_fetch(hass):
    coordinator, session = account(hass)
    session.async_get_parcels.return_value = {
        "totalCount": 1,
        "items": [raw_parcel(delivered=True)],
    }
    await coordinator._async_update_data()
    assert len(coordinator.delivered) == 1
    assert coordinator.delivered_codes == set()


@pytest.mark.parametrize(("retry_after", "expected"), [(120, 120), (None, 3600)])
async def test_rate_limit_uses_homeassistant_retry_after(hass, retry_after, expected):
    coordinator, session = account(hass)
    session.async_get_parcels.side_effect = GlsDkApiError(
        "rate limited", status_code=429, retry_after=retry_after
    )
    with pytest.raises(UpdateFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.retry_after == expected
