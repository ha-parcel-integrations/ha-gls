"""Tests for GLS Netherlands mapping/status logic (moved out of test_coordinator.py
as part of the ``countries/`` restructure — imports and the renamed
``map_parcel_status`` → ``map_parcel_status_nl``/``normalize_parcel`` →
``normalize_parcel_nl`` call sites changed; assertions did not.
"""
import pytest

from custom_components.gls.const import ParcelStatus
from custom_components.gls.countries.nl import (
    build_history,
    map_event_status,
    map_parcel_status_nl,
    normalize_parcel_nl,
)

from ..payloads import active_sample, delivered_sample

# ---------------------------------------------------------------------------
# map_parcel_status_nl / map_event_status
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
    assert map_parcel_status_nl(state) == expected


def test_map_parcel_status_none_is_unknown():
    assert map_parcel_status_nl(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status_nl(99) == ParcelStatus.UNKNOWN


def test_map_event_status_none_and_unmapped():
    assert map_event_status(None) is None
    assert map_event_status(98) is None
    assert map_event_status(4) == ParcelStatus.DELIVERED


def test_unmapped_state_warns_only_once():
    # Second call hits the "already logged" early return branch.
    assert map_parcel_status_nl(97) == ParcelStatus.UNKNOWN
    assert map_parcel_status_nl(97) == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_maps_scans_oldest_to_newest():
    history = build_history(delivered_sample()["scans"])
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
# normalize_parcel_nl
# ---------------------------------------------------------------------------


def test_normalize_delivered_parcel():
    parcel = normalize_parcel_nl(delivered_sample())
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
    parcel = normalize_parcel_nl(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 5
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_window():
    parcel = normalize_parcel_nl(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-04-29T13:00:00Z"
    assert parcel["planned_to"] == "2026-04-29T15:00:00Z"


def test_normalize_pending_placeholder():
    parcel = normalize_parcel_nl({"parcelNo": "123", "state": None})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_delivered_via_scan_flag_without_state():
    raw = delivered_sample()
    raw["state"] = None
    parcel = normalize_parcel_nl(raw)
    assert parcel["delivered"] is True  # deliveryScanInfo.isDelivered


def test_tracking_url_nl_uses_country_site_with_postcode():
    """NL parcels deep-link to gls-info.nl with the postcode (the generic
    gls-group.com link intermittently returns 'package not found' for NL)."""
    parcel = normalize_parcel_nl(delivered_sample(), postal_code="2841XC", country="NL")
    assert parcel["url"] == (
        "https://www.gls-info.nl/tracking?trackid=0085105093278&zipcode=2841XC"
    )


def test_tracking_url_falls_back_without_postcode():
    """Without a postcode the NL country-site link cannot be built, so the
    generic link is used."""
    parcel = normalize_parcel_nl(delivered_sample(), country="NL")
    assert parcel["url"] == (
        "https://gls-group.com/GROUP/en/parcel-tracking?match=0085105093278"
    )


def test_normalize_parcel_partial_dimensions_have_no_text():
    """A partial dimensions payload must not render 'None' into the text."""
    sample = active_sample()
    sample["width"] = None
    sample["height"] = None
    parcel = normalize_parcel_nl(sample)
    assert parcel["dimensions"]["length"] == 34
    assert parcel["dimensions"]["text"] is None


def test_normalize_parcel_no_dimensions_at_all():
    sample = active_sample()
    sample["length"] = sample["width"] = sample["height"] = None
    parcel = normalize_parcel_nl(sample)
    assert parcel["dimensions"] is None
