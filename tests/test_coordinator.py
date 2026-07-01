"""Tests for the GLS coordinator logic."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.api import GlsApiError
from custom_components.gls.const import (
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.gls.coordinator import (
    GlsCoordinator,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    sort_parcels_by_ts,
)


def _delivered_sample(parcel_no: str = "0085105093278") -> dict:
    """A trimmed real GLS-NL details response for a delivered parcel."""
    return {
        "parcelNo": parcel_no,
        "state": 4,
        "suppliedWeight": 0.1,
        "weighedWeight": None,
        "width": 25,
        "height": 5,
        "length": 34,
        "isPickup": False,
        "addressInfo": {
            "from": {"name": "get your goods GmbH"},
            "to": {"name": "John Doe"},
        },
        "deliveryScanInfo": {
            "isDelivered": True,
            "dateTime": "2026-04-29T13:12:42",
            "parcelShop": None,
        },
        "deliveryStatus": {
            "etaTimestampMin": None,
            "etaTimestampMax": None,
        },
        "deliveryListInfo": {"isParcelShop": False},
        "parcels": [{"lastStatus": "Afgeleverd"}],
        "scans": [
            {"dateTime": "2026-04-24T10:38:50", "state": 0, "eventReasonDescr": "Aangekondigd bij GLS"},
            {"dateTime": "2026-04-27T23:03:58", "state": 1, "eventReasonDescr": "Pakket ontvangen door GLS"},
            {"dateTime": "2026-04-28T15:52:17", "state": 2, "eventReasonDescr": "Aangekomen op GLS depot"},
            {"dateTime": "2026-04-29T08:46:00", "state": 3, "eventReasonDescr": "Onderweg - geladen voor aflevering"},
            {"dateTime": "2026-04-29T13:12:42", "state": 4, "eventReasonDescr": "Afgeleverd"},
        ],
    }


def _active_sample(parcel_no: str = "1111111111111") -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = _delivered_sample(parcel_no)
    sample["state"] = 3
    sample["deliveryScanInfo"] = {"isDelivered": False, "dateTime": None, "parcelShop": None}
    sample["deliveryStatus"] = {
        "etaTimestampMin": "2026-04-29T13:00:00Z",
        "etaTimestampMax": "2026-04-29T15:00:00Z",
    }
    sample["parcels"] = [{"lastStatus": "Onderweg"}]
    return sample


# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,expected",
    [
        (0, ParcelStatus.REGISTERED),
        (1, ParcelStatus.IN_TRANSIT),
        (2, ParcelStatus.IN_TRANSIT),
        (3, ParcelStatus.OUT_FOR_DELIVERY),
        (4, ParcelStatus.DELIVERED),
    ],
)
def test_map_parcel_status_known(state, expected):
    assert map_parcel_status(state) == expected


def test_map_parcel_status_none_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status(99) == ParcelStatus.UNKNOWN


def test_map_event_status_none_and_unmapped():
    assert map_event_status(None) is None
    assert map_event_status(98) is None
    assert map_event_status(4) == ParcelStatus.DELIVERED


def test_unmapped_state_warns_only_once():
    # Second call hits the "already logged" early return branch.
    assert map_parcel_status(97) == ParcelStatus.UNKNOWN
    assert map_parcel_status(97) == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_maps_scans_oldest_to_newest():
    history = build_history(_delivered_sample()["scans"])
    assert len(history) == 5
    assert history[0]["raw_status"] == "Aangekondigd bij GLS"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    scans = [
        {"dateTime": f"2026-04-{d:02d}T10:00:00", "state": 1, "eventReasonDescr": "x"}
        for d in range(1, 26)
    ]
    assert len(build_history(scans, max_events=20)) == 20


def test_build_history_handles_missing_and_empty():
    assert build_history(None) == []
    assert build_history([{"state": 1}]) == []  # no timestamp -> skipped


def test_build_history_keeps_unparseable_timestamp_last():
    scans = [
        {"dateTime": "2026-04-24T10:00:00", "state": 1, "eventReasonDescr": "ok"},
        {"dateTime": "not-a-date", "state": 2, "eventReasonDescr": "weird"},
    ]
    history = build_history(scans)
    assert len(history) == 2
    assert history[-1]["raw_status"] == "weird"


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(_delivered_sample())
    assert parcel["carrier"] == "GLS"
    assert parcel["barcode"] == "0085105093278"
    assert parcel["sender"] == "get your goods GmbH"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Afgeleverd"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["weight"] == 0.1
    assert parcel["dimensions"]["text"] == "34 x 25 x 5 cm"
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_opt_in():
    parcel = normalize_parcel(_delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 5
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_window():
    parcel = normalize_parcel(_active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-04-29T13:00:00Z"
    assert parcel["planned_to"] == "2026-04-29T15:00:00Z"


def test_normalize_pending_placeholder():
    parcel = normalize_parcel({"parcelNo": "123", "state": None})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_delivered_via_scan_flag_without_state():
    raw = _delivered_sample()
    raw["state"] = None
    parcel = normalize_parcel(raw)
    assert parcel["delivered"] is True  # deliveryScanInfo.isDelivered


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# GlsCoordinator._async_update_data
# ---------------------------------------------------------------------------


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={CONF_PARCELS: parcels},
        unique_id=DOMAIN,
    )


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with([
        {CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"},
        {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"},
    ])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda no, pc: (
        _active_sample() if no == "1111111111111" else _delivered_sample()
    )
    coordinator = GlsCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == "1111111111111"
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_204_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_PARCEL_NO: "9999999999999", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # 204
    coordinator = GlsCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_on_error(hass):
    entry = _entry_with([{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = GlsCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates cache

    client.async_get_parcel.side_effect = GlsApiError(500)
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_all_fail_raises(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = GlsApiError(500)
    coordinator = GlsCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_fields(hass):
    entry = _entry_with([
        {CONF_PARCEL_NO: "", CONF_POSTAL_CODE: "1234AB"},  # skipped
        {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"},
    ])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = GlsCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = GlsCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    in_transit = _active_sample("1111111111111")
    in_transit["state"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _active_sample("1111111111111")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_update_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _active_sample()
    coordinator = GlsCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    # First refresh: in_transit (state 2), events suppressed.
    in_transit = _active_sample()
    in_transit["state"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()

    # Second refresh: out_for_delivery (state 3) — still active, status changed.
    client.async_get_parcel.return_value = _active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY
