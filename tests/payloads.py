"""Sample GLS API payloads shared by the test modules.

Trimmed from a real ``/details`` response for an NL parcel — the fields the
integration reads, plus the blocks diagnostics must redact. Keep them here
rather than inline in each test module: when the payload shape turns out to
differ from what we assumed, there is then exactly one place to fix.

Every helper returns a **fresh** dict, so a test may mutate what it gets back
without leaking into the next one.
"""
from __future__ import annotations

ACTIVE_NO = "1111111111111"
DELIVERED_NO = "0085105093278"


def scan(date_time: str, state: int, description: str) -> dict:
    """One entry of GLS's own ``scans`` timeline."""
    return {
        "dateTime": date_time,
        "state": state,
        "eventReasonDescr": description,
    }


def delivered_sample(parcel_no: str = DELIVERED_NO) -> dict:
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
            scan("2026-04-24T10:38:50", 0, "Aangekondigd bij GLS"),
            scan("2026-04-27T23:03:58", 1, "Pakket ontvangen door GLS"),
            scan("2026-04-28T15:52:17", 2, "Aangekomen op GLS depot"),
            scan("2026-04-29T08:46:00", 3, "Onderweg - geladen voor aflevering"),
            scan("2026-04-29T13:12:42", 4, "Afgeleverd"),
        ],
    }


def active_sample(parcel_no: str = ACTIVE_NO) -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = delivered_sample(parcel_no)
    sample["state"] = 3
    sample["deliveryScanInfo"] = {"isDelivered": False, "dateTime": None, "parcelShop": None}
    sample["deliveryStatus"] = {
        "etaTimestampMin": "2026-04-29T13:00:00Z",
        "etaTimestampMax": "2026-04-29T15:00:00Z",
    }
    sample["parcels"] = [{"lastStatus": "Onderweg"}]
    return sample


def minimal_sample(parcel_no: str = DELIVERED_NO) -> dict:
    """The smallest payload that still drives a full setup.

    Only the keys the coordinator actually dereferences, with a half-open ETA
    window and a single scan — enough for the entity and event paths without
    committing those tests to the full response shape.
    """
    return {
        "parcelNo": parcel_no,
        "state": 3,
        "addressInfo": {"from": {"name": "Sender"}, "to": {"name": "R"}},
        "deliveryScanInfo": {"isDelivered": False, "dateTime": None},
        "deliveryStatus": {
            "etaTimestampMin": "2026-05-01T10:00:00Z",
            "etaTimestampMax": None,
        },
        "parcels": [{"lastStatus": "Onderweg"}],
        "scans": [scan("2026-04-30T10:00:00", 1, "x")],
    }


def minimal_no_eta_sample(parcel_no: str = DELIVERED_NO) -> dict:
    """As :func:`minimal_sample`, but with no ETA and an empty timeline."""
    sample = minimal_sample(parcel_no)
    sample["deliveryStatus"] = {"etaTimestampMin": None, "etaTimestampMax": None}
    sample["scans"] = []
    return sample
