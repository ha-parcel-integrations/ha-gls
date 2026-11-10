"""Tests for GLS setup and unload."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
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
from custom_components.gls.countries.dk.session import (
    GlsDkApiError,
    GlsDkAuthError,
    GlsDkSession,
)

from .payloads import minimal_sample
from .test_account_coordinator import raw_parcel


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}
            ]
        },
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
    incoming = hass.states.get("sensor.gls_nl_1234ab_incoming_parcels")
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
        options={
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}
            ]
        },
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
        options={
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}
            ]
        },
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


async def test_legacy_unique_id_migrates_to_country_scoped_postcode(hass):
    """Pre-multi-hub entries (unique_id == DOMAIN) migrate straight to the
       country-scoped f"{country}:{postal_code}" unique_id
    in one hop — not to the bare postcode
       as an intermediate step — so the flow's per-postcode-and-country
       duplicate guard also covers them.
    """
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

    assert entry.unique_id == "NL:1234AB"

    # A second hub for the same postcode+country now aborts instead of
    # duplicating.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_bare_postcode_unique_id_migrates_to_country_scoped(hass):
    """1.5.1-and-earlier entries carry a bare-postcode unique_id (no legacy
    DOMAIN id involved) — these must also re-key to
    f"{country}:{postal_code}", the migration this build adds.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="12345",
        options={
            CONF_COUNTRY: "DE",
            CONF_PARCELS: [],
            CONF_POSTAL_CODE: "12345",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == "DE:12345"


async def test_already_country_scoped_unique_id_is_left_alone(hass):
    """An entry already on the current scheme must not be touched again —
    the migration is idempotent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:1234AB",
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: "1234AB"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == "NL:1234AB"


async def test_same_postcode_different_country_hubs_do_not_collide(hass):
    """The exact collision the country-scoped unique_id exists to fix: French and Italian postcodes can
    both read "39100" (both ``^\\d{5}$``) — a bare-postcode unique_id would
    reject the second hub as a duplicate of the first. Country-scoping must
    let both stand. (DE is avoided here since its setup step performs a live
    registration call.)
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "it", CONF_POSTAL_CODE: "39100"}
    )
    assert result["type"] == "create_entry"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "fr", CONF_POSTAL_CODE: "39100"}
    )
    assert result["type"] == "create_entry"


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
            CONF_PARCELS: [{CONF_PARCEL_NO: "075624238061", CONF_POSTAL_CODE: "00000"}],
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

    incoming = hass.states.get("sensor.gls_de_00000_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "0"  # delivered, not active
    delivered = hass.states.get("sensor.gls_de_00000_delivered_parcels")
    assert delivered is not None
    assert delivered.state == "1"

    # The learned parcelNumber was persisted (the cold-restart gap fix).
    assert entry.options[CONF_PARCELS][0][CONF_DE_PARCEL_NUMBER] == "YOXVB8CE"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_cz_setup_wires_group_locale_end_to_end(hass):
    """The full country="CZ" wiring (__init__.py -> api.py -> countries/group)
    — proves ``group_locale`` (not ``culture``, which CZ's COUNTRIES row
    doesn't define) actually reaches the transport, and that barcode comes
    from the tracked parcel_no rather than the response's own tuNo.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="CZ:11000",
        options={
            CONF_COUNTRY: "CZ",
            CONF_PARCELS: [{CONF_PARCEL_NO: "1234567890", CONF_POSTAL_CODE: "11000"}],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)

    cz_payload = {
        "tuNo": "1234567890",
        "referenceNo": "1234567890",
        "date": "2026-06-24",
        "progressBar": {
            "statusInfo": "INTRANSIT",
            "statusText": "In transit",
            "retourFlag": False,
        },
    }
    with patch(
        "custom_components.gls.api.async_get_parcel_group",
        new=AsyncMock(return_value=cz_payload),
    ) as mock_transport:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    mock_transport.assert_awaited_once_with(
        mock_transport.call_args.args[0],
        "gls-group.com",
        "CZ/en",
        "1234567890",
        "11000",
        country="CZ",
    )

    incoming = hass.states.get("sensor.gls_cz_11000_incoming_parcels")
    assert incoming is not None


async def test_data_only_entry_write_does_not_count_as_an_options_edit(hass):
    """Persisting DE session state alone must not poll.

    Every ``async_update_entry`` notifies the update listener, and
    ``async_request_refresh`` ignores Home Assistant's "Enable polling for
    updates" system option — so a write that changed no option must stay
    silent. A newly learned ``parcelNumber`` does change the options and
    still refreshes.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}
            ]
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.gls.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        refresh = AsyncMock()
        with patch.object(
            entry.runtime_data.coordinator, "async_request_refresh", new=refresh
        ):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DE_APP_INSTANCE_ID: "fresh"}
            )
            await hass.async_block_till_done()
            assert refresh.call_count == 0

            hass.config_entries.async_update_entry(
                entry,
                options={
                    **entry.options,
                    CONF_PARCELS: [
                        {
                            CONF_PARCEL_NO: "0085105093278",
                            CONF_POSTAL_CODE: "1234AB",
                            CONF_DE_PARCEL_NUMBER: "12345678",
                        }
                    ],
                },
            )
            await hass.async_block_till_done()
            assert refresh.call_count == 1


async def test_account_setup_registers_no_update_listener_and_ignores_rotation(hass):

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-firebase-user",
        title="GLS Denmark account",
        data={
            "source": "dk_account",
            "uid": "example-firebase-user",
            "email": "parcel@example.test",
            "refresh_token": "original-refresh",
        },
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    session = GlsDkSession(
        None, uid="example-firebase-user", refresh_token="original-refresh"
    )
    with (
        patch(
            "custom_components.gls.GlsDkSession", return_value=session
        ) as constructor,
        patch.object(
            session,
            "async_get_parcels",
            new=AsyncMock(return_value={"totalCount": 1, "items": [raw_parcel()]}),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.update_listeners == []
        coordinator = entry.runtime_data.coordinator
        token_updated = constructor.call_args.kwargs["token_updated"]
        with patch.object(
            coordinator, "async_request_refresh", new=AsyncMock()
        ) as refresh:
            token_updated(
                {
                    "uid": "example-firebase-user",
                    "email": "",
                    "refresh_token": "rotated-refresh",
                }
            )
            await hass.async_block_till_done()
            assert entry.data["email"] == "parcel@example.test"
            assert entry.data["refresh_token"] == "rotated-refresh"
            token_updated(
                {
                    "uid": "example-firebase-user",
                    "email": "parcel@example.test",
                    "refresh_token": "rotated-refresh",
                }
            )
            await hass.async_block_till_done()
            refresh.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_account_device_and_entity_ids_never_carry_the_email(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-firebase-user",
        title="GLS Denmark account",
        data={
            "source": "dk_account",
            "uid": "example-firebase-user",
            "email": "mailbox-owner@sample.test",
            "refresh_token": "original-refresh",
        },
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.countries.dk.session.GlsDkSession.async_get_parcels",
        new=AsyncMock(return_value={"totalCount": 1, "items": [raw_parcel()]}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [device.name for device in devices] == ["GLS Denmark"]
    entity_ids = [
        item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    ]
    assert "sensor.gls_denmark_incoming_parcels" in entity_ids
    assert not any(
        slug in entity_id for slug in ("mailbox", "sample") for entity_id in entity_ids
    )


async def test_account_first_refresh_auth_failure_does_not_forward_platforms(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-user",
        data={
            "source": "dk_account",
            "uid": "example-user",
            "refresh_token": "revoked",
        },
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.gls.countries.dk.session.GlsDkSession.async_get_parcels",
            new=AsyncMock(side_effect=GlsDkAuthError("sign-in required")),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    forward.assert_not_awaited()


async def test_manual_and_account_can_unload_independently(hass):
    manual = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:1234AB",
        options={CONF_COUNTRY: "NL", CONF_POSTAL_CODE: "1234AB", CONF_PARCELS: []},
    )
    account = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-firebase-user",
        data={
            "source": "dk_account",
            "uid": "example-firebase-user",
            "refresh_token": "example-refresh",
        },
        options={CONF_COUNTRY: "DK"},
    )
    manual.add_to_hass(hass)
    account.add_to_hass(hass)
    with patch(
        "custom_components.gls.countries.dk.session.GlsDkSession.async_get_parcels",
        new=AsyncMock(return_value={"totalCount": 1, "items": [raw_parcel()]}),
    ):
        assert await hass.config_entries.async_setup(manual.entry_id)
        await hass.async_block_till_done()
        assert account.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(account.entry_id)
        assert hass.services.has_service(DOMAIN, "track_parcel")
        assert manual.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(manual.entry_id)
        assert not hass.services.has_service(DOMAIN, "track_parcel")


def _session_that_the_backend_rejects(status):
    async def request(self, method, url, *, firebase=False, **kwargs):
        if firebase:
            return {
                "user_id": "example-firebase-user",
                "id_token": "synthetic-id-token",
                "refresh_token": "original-refresh",
                "expires_in": "3600",
                "token_type": "Bearer",
            }
        raise GlsDkApiError("rejected", status_code=status)

    return patch(
        "custom_components.gls.countries.dk.session.GlsDkSession._request",
        new=request,
    )


def _account_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="dk_account:example-firebase-user",
        data={
            "source": "dk_account",
            "uid": "example-firebase-user",
            "refresh_token": "original-refresh",
        },
        options={CONF_COUNTRY: "DK"},
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_account_setup_retries_when_the_backend_rejects_the_token(hass, status):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with _session_that_the_backend_rejects(status):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


@pytest.mark.parametrize("status", [401, 403])
async def test_loaded_account_keeps_its_data_when_the_backend_rejects_the_token(
    hass, status
):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.countries.dk.session.GlsDkSession.async_get_parcels",
        new=AsyncMock(return_value={"totalCount": 1, "items": [raw_parcel()]}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    with _session_that_the_backend_rejects(status):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert len(coordinator.data) == 1
    assert entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
