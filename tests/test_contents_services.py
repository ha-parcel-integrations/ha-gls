"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.gls.coordinator import GlsBaseCoordinator
from custom_components.gls.tracking import coordinator as module
from tests.account.test_coordinator import raw_parcel
from tests.payloads import ACTIVE_NO, DELIVERED_NO, active_sample, delivered_sample

CLIENT = "custom_components.gls.tracking.api.GlsApiClient"
ACCOUNT_FEED = "custom_components.gls.account.countries.dk.session.GlsDkSession"
REFRESH = f"{GlsBaseCoordinator.__module__}.GlsBaseCoordinator"
UNTRACKED = "5555555555555"
ACCOUNT_CODE = "EXAMPLE123456"
STORE_KEY = f"{DOMAIN}.contents.%s"


async def _setup(hass, codes: list[str] | None = None, postcode="1234AB"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"NL:{postcode}",
        options={
            CONF_COUNTRY: "NL",
            CONF_POSTAL_CODE: postcode,
            CONF_PARCELS: [{CONF_PARCEL_NO: code} for code in codes or []],
        },
    )
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code, postal: active_sample(code)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-firebase-user",
        data={
            "source": "account",
            "uid": "example-firebase-user",
            "refresh_token": "r",
        },
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    with patch(
        f"{ACCOUNT_FEED}.async_get_parcels",
        new=AsyncMock(
            return_value={"totalCount": 1, "items": [raw_parcel(ACCOUNT_CODE)]}
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@contextmanager
def _polls():
    with (
        patch(
            f"{CLIENT}.async_get_parcel",
            new=AsyncMock(side_effect=lambda code, postal: active_sample(code)),
        ),
        patch(
            f"{ACCOUNT_FEED}.async_get_parcels",
            new=AsyncMock(
                return_value={"totalCount": 1, "items": [raw_parcel(ACCOUNT_CODE)]}
            ),
        ),
    ):
        yield


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


@asynccontextmanager
async def _no_poll():
    """Patch every way a poll could reach GLS, so a test can assert none happened."""
    with (
        patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh,
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch(f"{ACCOUNT_FEED}.async_get_parcels", new=AsyncMock()) as feed,
    ):
        yield refresh, fetch, feed


async def _call_quiet(hass, service: str, data: dict) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)
    await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


def _store(entry: MockConfigEntry):
    return entry.runtime_data.coordinator.contents_store


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    assert _contents(entry, ACTIVE_NO) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [ACTIVE_NO])

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": " Running  shoes "},
    )
    assert _contents(entry, ACTIVE_NO) == "Running shoes"

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Winter socks"},
    )
    assert _contents(entry, ACTIVE_NO) == "Winter socks"

    await _call(
        hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "  "}
    )
    assert _contents(entry, ACTIVE_NO) is None


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_NO})

    assert _contents(entry, ACTIVE_NO) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, ["EXAMPLE000999"])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: " example000999 ", "contents": "Running shoes"},
    )
    assert _store(entry).get("EXAMPLE000999") == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_PARCEL_NO: ACTIVE_NO}]


async def test_update_contents_updates_listeners_only_when_something_changed(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    seen = []
    coordinator.async_add_listener(lambda: seen.append(1))
    data = {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"}
    async with _no_poll() as (refresh, fetch, feed):
        await _call_quiet(hass, "update_contents", data)
        assert len(seen) == 1
        await _call_quiet(hass, "update_contents", data)
        assert len(seen) == 1
        await _call_quiet(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert len(seen) == 1
        await _call_quiet(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_NO})
        assert len(seen) == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    feed.assert_not_called()
    assert _contents(entry, ACTIVE_NO) is None


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(ACTIVE_NO, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(ACTIVE_NO, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_NO},
            blocking=True,
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_PARCEL_NO: ACTIVE_NO}]
    assert _contents(entry, ACTIVE_NO) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_NO})

    assert _store(entry).get(ACTIVE_NO) is None
    assert _contents(entry, ACTIVE_NO) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    track = {CONF_TRACKING_CODE: ACTIVE_NO}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, ACTIVE_NO) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_NO) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, ACTIVE_NO) is None
    assert entry.options[CONF_PARCELS] == [{CONF_PARCEL_NO: ACTIVE_NO}]


async def test_track_parcel_contents_change_does_not_poll(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    seen = []
    coordinator.async_add_listener(lambda: seen.append(1))
    data = {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"}
    async with _no_poll() as (refresh, fetch, feed):
        await _call_quiet(hass, "track_parcel", data)
        await _call_quiet(hass, "track_parcel", data)
        assert len(seen) == 1
        await _call_quiet(hass, "track_parcel", {**data, "contents": ""})
        assert len(seen) == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    feed.assert_not_called()


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_NO})

    assert _store(entry).get(ACTIVE_NO) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    asleep = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    await _setup(hass, [], postcode="2000BB")
    assert await hass.config_entries.async_unload(asleep.entry_id)

    await hass.services.async_call(
        DOMAIN, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_NO}, blocking=True
    )

    assert asleep.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_NO])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, ACTIVE_NO) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup(hass, [ACTIVE_NO])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, ACTIVE_NO) is None


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_NO])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )
    store = _store(entry)
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [ACTIVE_NO])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    assert events == []


# ---------------------------------------------------------------------------
# overlay on the coordinators
# ---------------------------------------------------------------------------


async def test_user_contents_reaches_the_delivered_list_and_events(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 10,
        },
    )
    delivered = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", delivered.append)

    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=delivered_sample(ACTIVE_NO)),
    ):
        await coordinator._async_update_data()
        await hass.async_block_till_done()

    assert coordinator.delivered[0]["contents"] == "Running shoes"
    assert delivered[0].data["contents"] == "Running shoes"


async def test_user_contents_is_keyed_on_the_requested_code(hass):
    """A payload carrying another number still finds the value stored for the code."""
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")

    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=active_sample(DELIVERED_NO)),
    ):
        data = await coordinator._async_update_data()

    assert data[0]["barcode"] == DELIVERED_NO
    assert data[0]["contents"] == "Running shoes"


async def test_user_contents_overlays_the_carrier_value(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator

    def carrier_text(raw, **kwargs):
        parcel = real(raw, **kwargs)
        parcel["contents"] = "Carrier text"
        return parcel

    real = module.normalize_parcel
    with (
        patch.object(module, "normalize_parcel", side_effect=carrier_text),
        patch(
            f"{CLIENT}.async_get_parcel",
            new=AsyncMock(return_value=active_sample(ACTIVE_NO)),
        ),
    ):
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )
        coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Running shoes"
        )
        coordinator.contents_store.async_set(ACTIVE_NO, None)
        assert (await coordinator._async_update_data())[0]["contents"] == (
            "Carrier text"
        )


async def test_known_codes_list_every_code_the_entry_holds(hass):
    hub = await _setup(hass, [ACTIVE_NO])
    account = await _setup_account(hass)

    assert hub.runtime_data.coordinator.known_codes == {ACTIVE_NO}
    assert account.runtime_data.coordinator.known_codes == {ACCOUNT_CODE}


async def test_account_entries_take_contents_too(hass):
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )

    assert _contents(account, ACCOUNT_CODE) == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    home = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    work = await _setup(hass, [DELIVERED_NO], postcode="2000BB")

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    assert _store(home).get(ACTIVE_NO) == "Running shoes"
    assert _store(work).get(ACTIVE_NO) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    home = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    work = await _setup(hass, [ACTIVE_NO], postcode="2000BB")

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
    )

    assert _store(home).get(ACTIVE_NO) == "Running shoes"
    assert _store(work).get(ACTIVE_NO) == "Running shoes"


async def test_update_contents_reaches_a_hub_and_an_account_alike(hass):
    hub = await _setup(hass, [ACCOUNT_CODE])
    account = await _setup_account(hass)

    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
    )

    assert _store(hub).get(ACCOUNT_CODE) == "Running shoes"
    assert _store(account).get(ACCOUNT_CODE) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    home = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    work = await _setup(hass, [], postcode="2000BB")

    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )

    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(home).get(UNTRACKED) is None
    assert _store(work).get(UNTRACKED) is None


async def test_config_entry_id_restricts_and_stores_an_unknown_code(hass):
    home = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    work = await _setup(hass, [], postcode="2000BB")

    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": work.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: ACTIVE_NO,
            "contents": "Winter socks",
            "config_entry_id": home.entry_id,
        },
    )

    assert _store(work).get(UNTRACKED) == "Running shoes"
    assert _store(home).get(UNTRACKED) is None
    assert _store(home).get(ACTIVE_NO) == "Winter socks"
    assert _store(work).get(ACTIVE_NO) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _setup(hass, [ACTIVE_NO])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_NO, "config_entry_id": "missing"},
        )


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_an_account_alone_registers_only_update_contents(hass):
    account = await _setup_account(hass)

    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    hub = await _setup(hass, [ACTIVE_NO])
    account = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(hub.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_hub_after_an_account_still_registers_the_hub_services(hass):
    await _setup_account(hass)
    await _setup(hass, [ACTIVE_NO])

    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")


# ---------------------------------------------------------------------------
# reapplying without a poll
# ---------------------------------------------------------------------------


async def test_update_contents_on_an_account_does_not_poll(hass):
    account = await _setup_account(hass)
    seen = []
    account.runtime_data.coordinator.async_add_listener(lambda: seen.append(1))
    async with _no_poll() as (refresh, fetch, feed):
        await _call_quiet(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_CODE, "contents": "Running shoes"},
        )
    assert _contents(account, ACCOUNT_CODE) == "Running shoes"
    assert len(seen) == 1
    refresh.assert_not_called()
    fetch.assert_not_called()
    feed.assert_not_called()


async def test_two_entries_listing_the_code_are_both_reapplied_without_a_poll(hass):
    home = await _setup(hass, [ACTIVE_NO], postcode="1000AA")
    work = await _setup(hass, [ACTIVE_NO], postcode="2000BB")
    seen = []
    for entry in (home, work):
        entry.runtime_data.coordinator.async_add_listener(lambda: seen.append(1))
    async with _no_poll() as (refresh, fetch, feed):
        await _call_quiet(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
        )
    assert _contents(home, ACTIVE_NO) == "Running shoes"
    assert _contents(work, ACTIVE_NO) == "Running shoes"
    assert len(seen) == 2
    refresh.assert_not_called()
    fetch.assert_not_called()
    feed.assert_not_called()


async def test_a_newly_tracked_code_still_refreshes(hass):
    entry = await _setup(hass)
    with patch(f"{REFRESH}.async_request_refresh", new=AsyncMock()) as refresh:
        await _call_quiet(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
        )
    assert refresh.await_count == 1
    assert _store(entry).get(ACTIVE_NO) == "Running shoes"


async def test_reapply_reaches_delivered_parcels(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 10,
        },
    )
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=delivered_sample(ACTIVE_NO)),
    ):
        await coordinator._async_update_data()
        coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert coordinator.delivered
    assert coordinator.delivered[0]["contents"] is None

    async with _no_poll() as (refresh, fetch, feed):
        await _call_quiet(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_NO, "contents": "Running shoes"},
        )
        assert coordinator.delivered[0]["contents"] == "Running shoes"
        await _call_quiet(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_NO})
        assert coordinator.delivered[0]["contents"] is None
    refresh.assert_not_called()
    fetch.assert_not_called()
    feed.assert_not_called()


async def test_a_barcode_less_parcel_keeps_its_contents_after_reapply_and_poll(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    nameless = active_sample(ACTIVE_NO)
    nameless["parcelNo"] = None
    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=nameless)):
        coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert coordinator.data[0]["barcode"] is None

    coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")
    coordinator.async_reapply_contents()
    assert coordinator.data[0]["contents"] == "Running shoes"

    with patch(f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=nameless)):
        data = await coordinator._async_update_data()
    assert data[0]["contents"] == "Running shoes"


async def test_a_barcode_that_differs_from_the_requested_code_keeps_the_text(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=active_sample(DELIVERED_NO)),
    ):
        coordinator.async_set_updated_data(await coordinator._async_update_data())
    coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")

    coordinator.async_reapply_contents()

    assert coordinator.data[0]["barcode"] == DELIVERED_NO
    assert coordinator.data[0]["contents"] == "Running shoes"


async def test_reapply_falls_back_to_the_barcode_for_an_unknown_parcel(hass):
    entry = await _setup(hass, [ACTIVE_NO])
    coordinator = entry.runtime_data.coordinator
    coordinator._parcel_codes = {}
    coordinator.contents_store.async_set(ACTIVE_NO, "Running shoes")

    coordinator.async_reapply_contents()

    assert coordinator.data[0]["contents"] == "Running shoes"
