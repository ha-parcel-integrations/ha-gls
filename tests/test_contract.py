"""The parcel contract's checks, run over every country's normalised samples."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.gls.account.countries.dk import normalize_parcel_dk
from custom_components.gls.account.countries.pl import normalize_parcel_pl_account
from custom_components.gls.account.countries.pl.session import DETAILS_KEY
from custom_components.gls.const import ParcelStatus
from custom_components.gls.delivery_window import (
    is_pickup,
    planned_for_status,
    planned_window,
)
from custom_components.gls.tracking.countries.ca import normalize_parcel_ca
from custom_components.gls.tracking.countries.de import normalize_parcel_de
from custom_components.gls.tracking.countries.es import normalize_parcel_es
from custom_components.gls.tracking.countries.group import normalize_parcel_group
from custom_components.gls.tracking.countries.nl import normalize_parcel_nl
from custom_components.gls.tracking.countries.pl import normalize_parcel_pl
from custom_components.gls.tracking.countries.us import normalize_parcel_us

from .tracking.countries.es.test_es import delivered_sample as es_delivered

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _nl(state=3, **extra):
    raw = {
        "parcelNo": "1",
        "state": state,
        "deliveryStatus": {
            "etaTimestampMin": "2099-05-22T13:00:00+02:00",
            "etaTimestampMax": "2099-05-22T15:00:00+02:00",
        },
        **extra,
    }
    return lambda: normalize_parcel_nl(raw)


def _group(status_info, history=(), retour=False):
    return lambda: normalize_parcel_group(
        {
            "progressBar": {"statusInfo": status_info, "retourFlag": retour},
            "history": list(history),
        },
        parcel_no="1",
    )


_LOCKER = {"date": "2099-05-22", "time": "10:00", "evtNo": "3.896", "evtDscr": "x"}
_HANDED = {"date": "2099-05-23", "time": "10:00", "evtNo": "3.0", "evtDscr": "x"}


def _pl(ident, *names):
    return lambda: normalize_parcel_pl(
        {
            "progressBarIdent": ident,
            "stateDate": "2099-05-22T10:00:00",
            "eventReasons": [{"packageStatusName": name} for name in names],
        },
        parcel_no="1",
    )


def _account_pl(ident, *names):
    statuses = [
        {
            "packageStatusName": name,
            "packageStatusDate": f"2099-05-2{i + 1}T10:00:00Z",
        }
        for i, name in enumerate(names)
    ]
    return lambda: normalize_parcel_pl_account(
        {
            "shipmentNo": "490000000001",
            "progressBarIdent": ident,
            DETAILS_KEY: {
                "trackingShipment": {},
                "trackingShipmentPackages": [{"packageStatuses": statuses}],
            },
        },
        include_history=True,
    )


SAMPLES = {
    "pl_account_collected": _account_pl("DELIVERED", "Gotowa do odbioru", "Doręczona"),
    "pl_account_home": _account_pl("DELIVERED", "W doręczeniu", "Doręczona"),
    "nl_active": _nl(),
    "nl_moment": _nl(deliveryStatus={"etaTimestampMin": "2099-05-22T13:00:00+02:00"}),
    "nl_delivered": _nl(4),
    "nl_flag": _nl(3, deliveryScanInfo={"isDelivered": True}),
    "nl_pickup": _nl(3, isPickup=True),
    "group_active": _group("INTRANSIT"),
    "group_at_pickup": _group("DELIVEREDPS"),
    "group_collected": _group("DELIVERED", [_HANDED, _LOCKER]),
    "group_home": _group("DELIVERED", [_HANDED]),
    "group_returned": _group("DELIVERED", retour=True),
    "pl_active": _pl("INDELIVERY"),
    "pl_at_pickup": _pl("DELIVEREDPS", "gotowa do odbioru"),
    "pl_collected": _pl("DELIVERED", "doręczona", "gotowa do odbioru"),
    "pl_home": _pl("DELIVERED", "doręczona"),
    "es_delivered": lambda: normalize_parcel_es(es_delivered(), parcel_no="1"),
    "de_bare": lambda: normalize_parcel_de({"trackingReference": "1"}),
    "dk_bare": lambda: normalize_parcel_dk({"parcelNumber": "1"}),
    "us_bare": lambda: normalize_parcel_us({}),
    "ca_bare": lambda: normalize_parcel_ca({}),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    if parcel["status"] is ParcelStatus.DELIVERED:
        assert parcel["delivered"] is True
    elif parcel["status"] is not ParcelStatus.RETURNING:
        assert parcel["delivered"] is False


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


@pytest.mark.parametrize(
    "name", ["group_collected", "pl_collected", "pl_account_collected"]
)
def test_a_collected_pickup_parcel_stays_pickup(name):
    parcel = SAMPLES[name]()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


@pytest.mark.parametrize("name", ["group_home", "pl_home", "pl_account_home"])
def test_a_home_delivery_is_not_pickup(name):
    assert SAMPLES[name]()["pickup"] is False


def test_nl_window_keeps_both_ends():
    parcel = SAMPLES["nl_active"]()
    assert parcel["planned_from"] == "2099-05-22T13:00:00+02:00"
    assert parcel["planned_to"] == "2099-05-22T15:00:00+02:00"


def test_nl_moment_ends_at_the_end_of_its_day():
    parcel = SAMPLES["nl_moment"]()
    assert parcel["planned_to"] == "2099-05-22T23:59:59+02:00"


def test_nl_delivered_flag_decides_the_status():
    parcel = SAMPLES["nl_flag"]()
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_group_return_the_carrier_calls_delivered_stays_returning_and_finished():
    parcel = SAMPLES["group_returned"]()
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is True


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
