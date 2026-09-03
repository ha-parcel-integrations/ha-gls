"""Tests for the GLS coordinator logic."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.api import GlsApiError
from custom_components.gls.const import (
    CAPABILITIES_BY_VARIANT,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    KNOWN_CAPABILITIES,
    MID_INTERVAL_MINUTES,
    REFRESH_INTERVAL_AUTO,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.gls.coordinator import (
    GlsCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _refresh_interval,
    _refresh_setting,
    _stagger_minutes,
)
from custom_components.gls.parcels import (
    normalize_parcel,
    sort_parcels_by_ts,
)

from .payloads import active_sample, delivered_sample

# NL's own map_parcel_status_nl/map_event_status/build_history/
# normalize_parcel_nl unit tests moved to tests/countries/test_nl.py as part
# of the countries/ restructure (BUILD_PLAN_DE.md's structural decision).
# What's left here is GlsCoordinator integration behaviour, plus the two
# generic (country-agnostic) helpers `normalize_parcel` (the dispatcher) and
# `sort_parcels_by_ts` that `coordinator.py` itself imports from `parcels.py`.

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
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


# ---------------------------------------------------------------------------
# Dynamic polling (Section 2.1, barcode-based) — pure helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def test_refresh_interval_starts_hot_when_auto():
    entry = _auto_entry_with([])
    assert _refresh_interval(entry).total_seconds() == HOT_INTERVAL_MINUTES * 60


def test_refresh_setting_passes_through_auto():
    entry = _auto_entry_with([])
    assert _refresh_setting(entry) == REFRESH_INTERVAL_AUTO


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


def test_tier_is_none_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "registered", "planned_from": None},
        {"status": "problem", "planned_from": None},
        {"status": "returning", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "in_transit", "planned_from": None},
        {"status": "out_for_delivery", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_next_update_interval_is_none_for_none_tier():
    assert _next_update_interval(datetime(2026, 1, 1, 12, tzinfo=UTC), None, "entry-1") is None


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Dynamic polling — wired into _async_update_data
# ---------------------------------------------------------------------------


def _auto_entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO,
        },
        unique_id=DOMAIN,
    )


async def test_auto_mode_stops_entirely_with_nothing_tracked(hass):
    entry = _auto_entry_with([])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = GlsCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval is None


async def test_auto_mode_is_hot_for_an_out_for_delivery_parcel(hass):
    entry = _auto_entry_with(
        [{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = GlsCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_fixed_mode_keeps_configured_interval(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: [],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_REFRESH_INTERVAL: 60,
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = GlsCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval == timedelta(minutes=60)


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with([
        {CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"},
        {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"},
    ])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda no, pc: (
        active_sample() if no == "1111111111111" else delivered_sample()
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
    client.async_get_parcel.return_value = delivered_sample()
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
    client.async_get_parcel.return_value = delivered_sample()
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

    in_transit = active_sample("1111111111111")
    in_transit["state"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = active_sample("1111111111111")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_update_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = GlsCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    # First refresh: in_transit (state 2), events suppressed.
    in_transit = active_sample()
    in_transit["state"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()

    # Second refresh: out_for_delivery (state 3) — still active, status changed.
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_update_fires_delivered_event_not_status_changed(hass):
    """The hop to delivered fires parcel_delivered — never status_changed."""
    entry = _entry_with([{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = GlsCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))

    client.async_get_parcel.return_value = active_sample("1111111111111")
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample("1111111111111")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == "1111111111111"
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires neither registered nor delivered."""
    entry = _entry_with([{CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda no, pc: (
        active_sample(no) if no == "1111111111111" else delivered_sample(no)
    )
    coordinator = GlsCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh: seeds state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"},
                {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_update_cached_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    entry = _entry_with([{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = GlsCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = GlsApiError(500)
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


async def test_delivered_filter_days_and_count(hass):
    from datetime import timedelta

    from custom_components.gls.const import (
        CONF_DELIVERED_FILTER_AMOUNT,
        CONF_DELIVERED_FILTER_TYPE,
    )

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    delivered = [
        {"barcode": "RECENT", "delivered_at": recent},
        {"barcode": "OLD", "delivered_at": old},
    ]

    entry = _entry_with([])
    entry.add_to_hass(hass)
    coordinator = GlsCoordinator(hass, AsyncMock(), entry)

    # days: 7-day window drops the 30-day-old one.
    hass.config_entries.async_update_entry(
        entry, options={CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7}
    )
    kept = coordinator._apply_delivered_filter(delivered)
    assert {p["barcode"] for p in kept} == {"RECENT"}

    # parcels: keep only the most recent 1.
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 1},
    )
    kept = coordinator._apply_delivered_filter(delivered)
    assert kept == delivered[:1]


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = GlsCoordinator(hass, client, entry)
    coordinator._raw_cache["gone"] = {"parcelNo": "gone", "state": 4}

    await coordinator._async_update_data()

    assert "gone" not in coordinator._raw_cache
    assert "0085105093278" in coordinator._raw_cache


async def test_update_fetches_parcels_concurrently(hass):
    """All tracked parcels are fetched via one gather, not one-by-one."""
    import asyncio

    entry = _entry_with([
        {CONF_PARCEL_NO: "1111111111111", CONF_POSTAL_CODE: "1234AB"},
        {CONF_PARCEL_NO: "0085105093278", CONF_POSTAL_CODE: "1234AB"},
    ])
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(no, pc):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return active_sample(no)

    client = AsyncMock()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = GlsCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert peak == 2


def test_normalize_parcel_partial_dimensions_have_no_text():
    """A partial dimensions payload must not render 'None' into the text."""
    sample = active_sample()
    sample["width"] = None
    sample["height"] = None
    parcel = normalize_parcel(sample)
    assert parcel["dimensions"]["length"] == 34
    assert parcel["dimensions"]["text"] is None


def test_normalize_parcel_no_dimensions_at_all():
    sample = active_sample()
    sample["length"] = sample["width"] = sample["height"] = None
    parcel = normalize_parcel(sample)
    assert parcel["dimensions"] is None


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    for variant, fields in CAPABILITIES_BY_VARIANT.items():
        assert fields <= KNOWN_CAPABILITIES, variant


# ---------------------------------------------------------------------------
# CZ dispatch — the coordinator threads the looked-up parcel_no into
# normalize_parcel_group, since a group-leaf barcode must never come from a
# raw response field (countries/group's docstring). NL/DE ignore parcel_no
# entirely, so this is CZ-only coverage; it does not touch either of them.
# ---------------------------------------------------------------------------


async def test_update_threads_parcel_no_into_normalize_parcel_cz(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_COUNTRY: "CZ",
            CONF_PARCELS: [{CONF_PARCEL_NO: "1234567890", CONF_POSTAL_CODE: "11000"}],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id="11000",
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    # tuNo/referenceNo deliberately do NOT match the tracked parcel_no, the
    # way group-rest.md documents happening for a non-Czech consignment on
    # the same leaves.
    client.async_get_parcel.return_value = {
        "tuNo": "UNRELATED-ID",
        "referenceNo": "UNRELATED-ID",
        "progressBar": {
            "statusInfo": "INTRANSIT",
            "statusText": "On the way",
            "retourFlag": False,
        },
    }
    coordinator = GlsCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["barcode"] == "1234567890"
    assert data[0]["status"] == ParcelStatus.IN_TRANSIT
