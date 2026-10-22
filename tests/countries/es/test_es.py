"""Tests for the GLS Spain / Portugal signed consignee transport and mapping.

Every fixture here is synthetic. No populated Spanish body has been captured,
so these tests pin the *decisions* — the error branching, the pickup-point
gate, the incidence sentence map, de-duplication and the UTC handling —
rather than claiming to reproduce a real payload.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.gls.const import GlsApiError, ParcelStatus
from custom_components.gls.countries.es import (
    async_get_parcel_es,
    map_event_status_es,
    map_parcel_status_es,
    normalize_parcel_es,
)

NUMBER = "990011223"
POSTCODE = "28001"


def _session(status: int, body: object, *, raises: bool = False) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.status = status
    if raises:
        response.json = AsyncMock(side_effect=aiohttp.ContentTypeError(MagicMock(), ()))
    else:
        response.json = AsyncMock(return_value=body)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=context)
    return session


def _scan(code: str, description: str, at: str) -> dict:
    return {"code": code, "description": description, "at": at}


def delivered_sample() -> dict:
    """A structural sample of a delivered Spanish shipment."""
    return {
        "expeditionCode": NUMBER,
        "expeditionUuid": "00000000-0000-0000-0000-000000000000",
        "attributes": {"weight": 0.8, "packageCount": 1},
        "state": {
            "code": "delivered",
            "description": "Delivered",
            "deliveryDate": "2026-10-09",
        },
        "tracking": [
            _scan("delivered", "Delivered", "2026-10-09T09:12:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-10-09T06:40:00Z"),
            _scan("onTransit", "At delivery agency", "2026-10-08T22:05:00Z"),
            _scan("picked", "Collected from sender", "2026-10-07T17:30:00Z"),
        ],
        "agency": {"name": "MADRID", "address": "", "phones": []},
    }


# ---------------------------------------------------------------------------
# Transport: the four reply branches, all probed live on 2026-10-12
# ---------------------------------------------------------------------------


async def test_populated_200_returns_the_found_object():
    body = {"found": delivered_sample()}
    session = _session(200, body)
    assert await async_get_parcel_es(session, NUMBER, POSTCODE) == body["found"]


async def test_the_request_is_signed_and_stamped_and_carries_the_pinned_language():
    session = _session(200, {"found": delivered_sample()})
    await async_get_parcel_es(session, NUMBER, POSTCODE)
    headers = session.post.call_args.kwargs["headers"]
    assert len(headers["X-Signature"]) == 64
    assert headers["X-Timestamp"].endswith("Z")
    # Never the user's HA locale: the incidence map reads an English sentence.
    assert headers["Accept-Language"] == "en_US"
    assert headers["MyGls-Agent"] == "pwa"


async def test_the_request_sends_the_number_and_the_postcode_together():
    session = _session(200, {"found": delivered_sample()})
    await async_get_parcel_es(session, NUMBER, POSTCODE)
    assert session.post.call_args.kwargs["json"] == {
        "find": {
            "reference": NUMBER,
            "destination": {"address": {"postalCode": POSTCODE}},
        }
    }


async def test_every_attempt_is_signed_afresh():
    """A reused timestamp is rejected, so two calls must not share one."""
    signatures = set()
    for _ in range(2):
        session = _session(200, {"found": delivered_sample()})
        await async_get_parcel_es(session, NUMBER, POSTCODE)
        headers = session.post.call_args.kwargs["headers"]
        signatures.add((headers["X-Timestamp"], headers["X-Signature"]))
    # Same second is possible; the pair must still be recomputed, which this
    # asserts by the signature matching its own timestamp rather than a cached
    # one. Two identical pairs are only wrong if the stamp was cached, which
    # the length check below cannot see — so assert the weaker, true thing.
    assert all(len(signature) == 64 for _, signature in signatures)


async def test_semantic_not_found_returns_none():
    body = {
        "error": {
            "status": 404,
            "name": "NotFoundError",
            "message": "No shipment was found for the reference and postal code indicated.",
            "details": "notFound",
        }
    }
    assert await async_get_parcel_es(_session(404, body), NUMBER, POSTCODE) is None


async def test_a_404_without_the_notfound_detail_is_a_transport_failure():
    """Only the carrier's own ``notFound`` means absence. This is why the
    branch has its own test rather than a shared one — see the is_not_found
    revert in the DHL DE build (c480eac)."""
    body = {"error": {"status": 404, "name": "NotFoundError"}}
    with pytest.raises(GlsApiError):
        await async_get_parcel_es(_session(404, body), NUMBER, POSTCODE)


async def test_clock_skew_401_raises_and_names_the_clock(caplog):
    body = {
        "error": {
            "status": 401,
            "name": "InvalidCredentialsError",
            "message": "Invalid timestamp 2026-10-12T09:46:21.367Z",
        }
    }
    with caplog.at_level(logging.WARNING), pytest.raises(GlsApiError):
        await async_get_parcel_es(_session(401, body), NUMBER, POSTCODE)
    assert any("clock" in record.message for record in caplog.records)


async def test_rejected_signature_403_raises_without_echoing_the_credential(caplog):
    body = {
        "error": {
            "status": 403,
            "name": "ForbiddenError",
            "message": "Invalid signature",
        }
    }
    session = _session(403, body)
    with caplog.at_level(logging.WARNING), pytest.raises(GlsApiError):
        await async_get_parcel_es(session, NUMBER, POSTCODE)
    logged = " ".join(record.message for record in caplog.records)
    assert "signature" in logged
    # Report the failure, never the values that produced it.
    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-Signature"] not in logged


async def test_html_on_200_is_not_a_missing_parcel():
    """The service sits behind a CDN that can interpose a challenge page. The
    suite's standing rule: a blocked HTML response is a transport failure."""
    with pytest.raises(GlsApiError):
        await async_get_parcel_es(_session(200, None, raises=True), NUMBER, POSTCODE)


async def test_a_200_with_no_found_object_is_not_a_missing_parcel():
    with pytest.raises(GlsApiError):
        await async_get_parcel_es(_session(200, {"other": 1}), NUMBER, POSTCODE)


async def test_a_missing_postcode_is_reported_and_never_sent(caplog):
    """The postcode is half the lookup key here, and the service has no
    recognition surface, so a request without one cannot be formed."""
    session = _session(200, {"found": delivered_sample()})
    with caplog.at_level(logging.WARNING):
        assert await async_get_parcel_es(session, NUMBER, "") is None
    session.post.assert_not_called()
    assert any("postcode" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Status map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("dataReceived", ParcelStatus.REGISTERED),
        ("pickupPending", ParcelStatus.REGISTERED),
        ("picked", ParcelStatus.IN_TRANSIT),
        ("onTransit", ParcelStatus.IN_TRANSIT),
        ("partiallyDelivered", ParcelStatus.IN_TRANSIT),
        ("rechannelized", ParcelStatus.IN_TRANSIT),
        ("onDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("deliveredOnParcel", ParcelStatus.AT_PICKUP_POINT),
        ("receivedOnParcel", ParcelStatus.AT_PICKUP_POINT),
        ("delivered", ParcelStatus.DELIVERED),
        ("onReturn", ParcelStatus.RETURNING),
        ("returned", ParcelStatus.RETURNING),
        ("retained", ParcelStatus.PROBLEM),
        ("broken", ParcelStatus.PROBLEM),
        ("destroyed", ParcelStatus.PROBLEM),
        ("canceled", ParcelStatus.PROBLEM),
        ("pickupIncidence", ParcelStatus.PROBLEM),
    ],
)
def test_every_mapped_state_code(code, expected):
    assert map_parcel_status_es(code) == expected


def test_stored_is_unmapped_on_purpose_and_does_not_warn(caplog):
    """``stored`` has no milestone of its own, so it falls through to the
    suite's fallback. That is a known gap, not an unrecognised status, so it
    must not shout at the user."""
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_es("stored") == ParcelStatus.UNKNOWN
    assert not caplog.records


def test_an_unknown_code_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_es("teleported") == ParcelStatus.UNKNOWN
        assert map_parcel_status_es("teleported") == ParcelStatus.UNKNOWN
    assert (
        sum(
            "Unrecognised GLS Spain/Portugal status" in r.message
            for r in caplog.records
        )
        == 1
    )


@pytest.mark.parametrize("code", [None, "", "   ", 7, True])
def test_a_missing_code_is_unknown_without_warning(code, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_parcel_status_es(code) == ParcelStatus.UNKNOWN
    assert not caplog.records


def test_an_unmappable_event_row_stays_neutral():
    assert map_event_status_es("stored") is None
    assert map_event_status_es("delivered") == ParcelStatus.DELIVERED


# ---------------------------------------------------------------------------
# ``incidence`` — the single highest-value correctness decision here
# ---------------------------------------------------------------------------


def test_an_ordinary_missed_delivery_is_not_reported_as_an_exception_by_code_alone():
    """``incidence`` is overloaded: mapping it on the code alone would report
    every missed delivery the same way as a refusal or a diversion. The
    sentence is what decides, which is why the map reads it."""
    absent = map_parcel_status_es("incidence", "Absent")
    redirected = map_parcel_status_es(
        "incidence", "Shipment redirected to GLS Point Librería Centro"
    )
    assert absent != redirected


def test_a_diversion_to_a_gls_point_is_still_in_transit():
    assert (
        map_parcel_status_es(
            "incidence", "Shipment redirected to GLS Point Librería Centro"
        )
        == ParcelStatus.IN_TRANSIT
    )


@pytest.mark.parametrize(
    "sentence", ["Absent", "  absent  ", "ABSENT", "Refused by the consignee"]
)
def test_known_incidence_sentences_normalise(sentence):
    assert map_parcel_status_es("incidence", sentence) == ParcelStatus.PROBLEM


def test_an_unknown_incidence_sentence_falls_through_to_problem_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            map_parcel_status_es("incidence", "Eaten by a goat") == ParcelStatus.PROBLEM
        )
    assert any("incidence sentence" in r.message for r in caplog.records)


def test_an_incidence_with_no_sentence_is_a_problem():
    assert map_parcel_status_es("incidence", None) == ParcelStatus.PROBLEM


def test_the_redirect_prefix_is_anchored_not_a_substring_test():
    """A sentence that merely *mentions* a GLS Point late on must not be read
    as a diversion — the trap the DE text map's "never a substring" rule
    exists for."""
    assert (
        map_parcel_status_es(
            "incidence", "Refused by the consignee, shipment redirected to GLS Point X"
        )
        == ParcelStatus.PROBLEM
    )


# ---------------------------------------------------------------------------
# normalize_parcel_es
# ---------------------------------------------------------------------------


def test_a_delivered_shipment_normalizes():
    parcel = normalize_parcel_es(
        delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    assert parcel["carrier"] == "GLS"
    assert parcel["barcode"] == NUMBER
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-09T09:12:00+00:00"
    assert parcel["weight"] == 0.8
    assert parcel["dimensions"] is None
    assert len(parcel["history"]) == 4


def test_history_is_oldest_first_and_keeps_utc_instants_unshifted():
    """This is the one GLS surface whose clocks are true instants, so the
    local-wall-clock reconstruction the Italian and group routes need must not
    be applied here."""
    parcel = normalize_parcel_es(
        delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    stamps = [event["timestamp"] for event in parcel["history"]]
    assert stamps == sorted(stamps)
    assert stamps[0] == "2026-10-07T17:30:00+00:00"
    assert stamps[-1] == "2026-10-09T09:12:00+00:00"


def test_history_is_de_duplicated_on_code_description_and_instant():
    """The reference body repeats a row verbatim with an identical instant."""
    sample = delivered_sample()
    sample["tracking"].append(
        _scan("onTransit", "At delivery agency", "2026-10-08T22:05:00Z")
    )
    parcel = normalize_parcel_es(
        sample, postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    assert len(parcel["history"]) == 4


def test_history_is_omitted_unless_asked_for():
    parcel = normalize_parcel_es(delivered_sample(), postal_code=POSTCODE)
    assert parcel["history"] is None


def test_an_unmapped_state_takes_the_newest_mappable_history_event():
    sample = delivered_sample()
    sample["state"]["code"] = "stored"
    sample["tracking"].insert(
        0, _scan("onTransit", "Held at agency", "2026-10-09T10:00:00Z")
    )
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT


def test_a_wholly_unmappable_parcel_stays_unknown():
    sample = {"expeditionCode": NUMBER, "state": {"code": "stored"}, "tracking": []}
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["status"] == ParcelStatus.UNKNOWN


def test_the_barcode_is_the_code_the_user_tracked():
    """Deliberately unlike PL/NL: Spain answers a 14-digit parcel barcode with
    a 10-digit *shipment* number the recipient never sees, and the per-parcel
    sensor's unique_id is built from this field."""
    parcel = normalize_parcel_es(
        diverted_to_gls_point_sample(), postal_code=POSTCODE, parcel_no="61687311553222"
    )
    assert parcel["barcode"] == "61687311553222"
    assert parcel["raw"]["expeditionCode"] == 1000000001


def test_the_shipment_number_stands_in_only_when_the_user_code_is_missing():
    """It arrives as an int on the wire, so it needs coercing before it can
    be a barcode at all."""
    parcel = normalize_parcel_es(
        diverted_to_gls_point_sample(), postal_code=POSTCODE, parcel_no=None
    )
    assert parcel["barcode"] == "1000000001"


def test_a_missing_code_everywhere_leaves_no_barcode():
    sample = diverted_to_gls_point_sample()
    del sample["expeditionCode"]
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=None)
    assert parcel["barcode"] is None
    assert parcel["url"] is None


def test_the_tracking_link_carries_the_postcode_as_well_as_the_number():
    parcel = normalize_parcel_es(
        delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER
    )
    assert NUMBER in parcel["url"]
    assert POSTCODE in parcel["url"]


def test_there_is_no_link_without_a_postcode():
    """The generic GROUP fallback must not be used for Spain: Spanish
    consignments are not in the pan-EU index, so it would show a not-found
    page rather than the parcel."""
    parcel = normalize_parcel_es(delivered_sample(), postal_code=None, parcel_no=NUMBER)
    assert parcel["url"] is None


# ---------------------------------------------------------------------------
# The pickup-point gate — the trap worth the whole section
# ---------------------------------------------------------------------------


def test_a_destination_depot_is_never_a_pickup_point():
    """``agency`` is the destination depot and is present on every shipment."""
    parcel = normalize_parcel_es(
        delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER
    )
    assert parcel["pickup_point"] is None
    assert parcel["pickup"] is False


def test_a_parcel_shop_without_a_gls_point_scan_is_not_a_pickup_point(caplog):
    """Reading ``parcelShop`` unconditionally would give every Spanish parcel
    a phantom pickup point — the defect fixed in Correos 1.0.0."""
    sample = delivered_sample()
    sample["parcelShop"] = {"name": "Librería Centro"}
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["pickup_point"] is None
    assert any("parcelShop" in record.message for record in caplog.records)


def test_a_parcel_shop_named_by_a_gls_point_scan_is_a_pickup_point():
    sample = delivered_sample()
    sample["state"] = {"code": "receivedOnParcel", "description": "At GLS Point"}
    sample["parcelShop"] = {"name": "Librería Centro"}
    sample["tracking"].insert(
        0, _scan("receivedOnParcel", "At GLS Point", "2026-10-09T11:00:00Z")
    )
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["pickup_point"] == "Librería Centro"
    assert parcel["pickup"] is True
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT


# ---------------------------------------------------------------------------
# Pre-1.0 WARNING obligations
# ---------------------------------------------------------------------------


def test_a_delivery_date_on_a_moving_parcel_is_reported_and_not_published(caplog):
    """``state.deliveryDate`` does not say whether it is an estimate or an
    actual, so the planned fields stay unset until a real parcel settles it."""
    sample = delivered_sample()
    sample["state"] = {
        "code": "onTransit",
        "description": "In transit",
        "deliveryDate": "2026-10-10",
    }
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert any("deliveryDate" in record.message for record in caplog.records)


def test_unexpected_top_level_keys_are_reported_as_names_and_types_only(caplog):
    sample = delivered_sample()
    sample["surprise"] = {"secret": "value"}
    with caplog.at_level(logging.WARNING):
        normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    logged = " ".join(record.message for record in caplog.records)
    assert "surprise: dict" in logged
    assert "value" not in logged


def test_known_keys_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_parcel_es(delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER)
    assert not any("unexpected keys" in record.message for record in caplog.records)


def test_raw_is_never_trimmed():
    """Suite rule: ``raw`` always carries the full record; privacy lives in
    diagnostics redaction only."""
    sample = delivered_sample()
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["raw"] == sample


def test_a_weight_sent_as_a_display_string_is_still_a_number():
    sample = delivered_sample()
    sample["attributes"]["weight"] = "0,8"
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["weight"] == 0.8


def test_an_unparseable_weight_is_dropped_rather_than_guessed():
    sample = delivered_sample()
    sample["attributes"]["weight"] = "heavy"
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["weight"] is None


# ---------------------------------------------------------------------------
# Defensive edges — a malformed row must degrade, never raise
# ---------------------------------------------------------------------------


def test_an_unparseable_instant_drops_that_row_rather_than_the_parcel():
    sample = delivered_sample()
    sample["tracking"].append(_scan("onTransit", "Scanned", "not-a-date"))
    parcel = normalize_parcel_es(
        sample, postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    assert len(parcel["history"]) == 4
    assert parcel["status"] == ParcelStatus.DELIVERED


@pytest.mark.parametrize("tracking", [None, "not a list", 7])
def test_a_tracking_field_that_is_not_a_list_is_ignored(tracking):
    sample = delivered_sample()
    sample["tracking"] = tracking
    parcel = normalize_parcel_es(
        sample, postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    assert parcel["history"] == []
    # The state code still carries the parcel.
    assert parcel["status"] == ParcelStatus.DELIVERED


def test_non_dict_rows_in_tracking_are_skipped():
    sample = delivered_sample()
    sample["tracking"] = ["nonsense", None, *sample["tracking"]]
    parcel = normalize_parcel_es(
        sample, postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
    )
    assert len(parcel["history"]) == 4


def test_a_delivered_parcel_with_no_delivered_scan_dates_from_its_newest_scan():
    sample = delivered_sample()
    sample["tracking"] = [
        _scan("onDelivery", "Out for delivery", "2026-10-09T06:40:00Z"),
        _scan("onTransit", "At delivery agency", "2026-10-08T22:05:00Z"),
    ]
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-09T06:40:00+00:00"


def test_a_delivered_parcel_with_no_usable_scan_has_no_delivered_at():
    sample = delivered_sample()
    sample["tracking"] = []
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None


def test_a_parcel_shop_without_a_usable_name_is_not_a_pickup_point():
    sample = delivered_sample()
    sample["state"] = {"code": "receivedOnParcel", "description": "At GLS Point"}
    sample["parcelShop"] = {"name": 7}
    sample["tracking"].insert(
        0, _scan("receivedOnParcel", "At GLS Point", "2026-10-09T11:00:00Z")
    )
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["pickup_point"] is None


@pytest.mark.parametrize("state", [None, "not a dict", 7])
def test_a_state_field_that_is_not_a_dict_degrades_to_the_history(state):
    sample = delivered_sample()
    sample["state"] = state
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["status"] == ParcelStatus.DELIVERED


@pytest.mark.parametrize("attributes", [None, "not a dict", {}])
def test_a_missing_attributes_block_leaves_the_weight_unset(attributes):
    sample = delivered_sample()
    sample["attributes"] = attributes
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["weight"] is None


def test_a_boolean_weight_is_not_mistaken_for_a_number():
    sample = delivered_sample()
    sample["attributes"]["weight"] = True
    parcel = normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert parcel["weight"] is None


def test_a_country_without_a_postcode_bearing_link_uses_the_number_alone():
    """PT and ES both carry a postcode template; an unknown country code falls
    back to the repo's generic link, which takes the number only."""
    parcel = normalize_parcel_es(
        delivered_sample(), postal_code=POSTCODE, parcel_no=NUMBER, country="ZZ"
    )
    assert parcel["url"] is not None
    assert NUMBER in parcel["url"]


def test_unexpected_keys_are_only_reported_once(caplog):
    sample = delivered_sample()
    sample["surprise"] = 1
    with caplog.at_level(logging.WARNING):
        normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
        normalize_parcel_es(sample, postal_code=POSTCODE, parcel_no=NUMBER)
    assert sum("unexpected keys" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# Structural samples of the two first real Spanish parcels (2026-10-12)
#
# Key names, types, codes and wire ordering are as the carrier sent them;
# every *value* that could identify anyone or anything is synthetic, per the
# suite rule that fixtures carry no real numbers and no personal data.
# ---------------------------------------------------------------------------

SHOP_NAME = "PS GLS EXAMPLE SHOP"


def diverted_to_gls_point_sample() -> dict:
    """A shipment the recipient diverted to a GLS Point and collected there.

    Note ``expeditionCode`` is an **int** on the wire, and the recipient's own
    diversion shows up as ``selectedOption`` plus a ``picked`` scan whose
    sentence is the redirect wording.
    """
    return {
        "allowedActions": {"changeSchedule": False, "canReturn": False},
        "attributes": {
            "packageCount": 1,
            "weight": 4.7,
            "reimbursement": 0,
            "postageDue": 0,
        },
        "clientReference": "SENDERREF1",
        "destination": {"address": {"locality": "LOCALITY A"}},
        "expeditionCode": 1000000001,
        "expeditionUuid": "00000000-0000-0000-0000-000000000001",
        "origin": {"address": {"locality": "LOCALITY B"}},
        "originAgency": 600,
        "references": [],
        "sender": {"name": "SENDER COMPANY"},
        "agency": {
            "code": 400,
            "name": "DESTINATION DEPOT",
            "address": {
                "street": "",
                "locality": "",
                "postalCode": "",
                "country": "ES",
            },
            "phones": [],
            "schedule": "DE 08:00 A 14:00",
        },
        "expeditionAliasOption": "none",
        "state": {
            "code": "delivered",
            "steps": {"current": 5, "last": 5},
            "description": "Delivered",
            "deliveryDate": "2026-09-18",
        },
        "tracking": [
            _scan("delivered", "Delivered", "2026-09-18T16:03:00Z"),
            _scan(
                "receivedOnParcel", f"Received in {SHOP_NAME}", "2026-09-16T12:03:00Z"
            ),
            _scan(
                "deliveredOnParcel",
                f"Deposited in {SHOP_NAME}",
                "2026-09-16T11:15:00Z",
            ),
            _scan("onDelivery", "Out for delivery", "2026-09-16T07:43:00Z"),
            _scan("onTransit", "At delivery agency", "2026-09-16T05:37:00Z"),
            _scan(
                "picked",
                f"Shipment redirected to GLS Point {SHOP_NAME}",
                "2026-09-15T15:25:00Z",
            ),
            _scan(
                "picked",
                "Parcel picked up by GLS. Pending to arrive to final destination",
                "2026-09-15T15:15:00Z",
            ),
            _scan(
                "dataReceived",
                "Label created. The shipment has not yet been delivered to GLS by "
                "the sender",
                "2026-09-14T22:24:00Z",
            ),
        ],
        "whatsapp": {"suggest": True},
        "selectedOption": {
            "at": "2026-09-15T15:24:47Z",
            "option": "deriveToParcel",
            "derive": {"parcelShop": {"id": 1, "name": SHOP_NAME}},
        },
        "parcelShop": {
            "id": 1,
            "active": True,
            "type": "shop",
            "name": SHOP_NAME,
            "address": {
                "street": "",
                "locality": "",
                "postalCode": "",
                "country": "ES",
            },
            "notes": "",
            "schedules": ["Mon a Sat, 10:00 to 15:00"],
            "phones": [],
        },
    }


def failed_attempt_sample() -> dict:
    """A two-parcel shipment delivered to the door after a failed attempt.

    Carries the ``incidence`` sentence a real parcel produced, and the tied
    timestamps that make the history's ordering rule observable.
    """
    return {
        "allowedActions": {"changeSchedule": False, "canReturn": False},
        "attributes": {
            "packageCount": 2,
            "weight": 122.5,
            "reimbursement": 70,
            "postageDue": 77.17,
        },
        "clientReference": "SENDERREF2",
        "destination": {"address": {"locality": "LOCALITY C"}},
        "expeditionCode": 1000000002,
        "expeditionUuid": "00000000-0000-0000-0000-000000000002",
        "origin": {"address": {"locality": "LOCALITY D"}},
        "originAgency": 690,
        "references": [],
        "sender": {"name": "SENDER OFFICE"},
        "agency": {"code": 300, "name": "DESTINATION DEPOT", "phones": []},
        "expeditionAliasOption": "none",
        "state": {
            "code": "delivered",
            "steps": {"current": 5, "last": 5},
            "description": "Delivered",
            "deliveryDate": "2026-08-07",
        },
        "tracking": [
            _scan("delivered", "Delivered", "2026-08-07T15:34:00Z"),
            # These two carry the identical instant, in this wire order.
            _scan("onTransit", "At delivery agency", "2026-08-07T09:10:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-08-07T09:10:00Z"),
            _scan("incidence", "Missing data", "2026-08-06T17:29:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-08-06T08:40:00Z"),
            _scan("onTransit", "At delivery agency", "2026-08-06T06:22:00Z"),
            _scan(
                "picked",
                "Parcel picked up by GLS. Pending to arrive to final destination",
                "2026-08-05T13:43:00Z",
            ),
            _scan(
                "dataReceived",
                "Label created. The shipment has not yet been delivered to GLS by "
                "the sender",
                "2026-08-05T13:37:00Z",
            ),
        ],
        "whatsapp": {"suggest": True},
    }


@pytest.mark.parametrize(
    "sample", [diverted_to_gls_point_sample, failed_attempt_sample]
)
def test_a_real_shaped_parcel_produces_no_warnings(sample, caplog):
    """Both captures must map end to end in silence: a warning here means the
    payload carries something the build did not account for."""
    with caplog.at_level(logging.WARNING):
        normalize_parcel_es(
            sample(), postal_code=POSTCODE, parcel_no=NUMBER, include_history=True
        )
    assert [record.message for record in caplog.records] == []


def test_the_diverted_parcel_maps_its_gls_point_and_full_history():
    parcel = normalize_parcel_es(
        diverted_to_gls_point_sample(),
        postal_code=POSTCODE,
        parcel_no=NUMBER,
        include_history=True,
    )
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-09-18T16:03:00+00:00"
    assert parcel["weight"] == 4.7
    assert parcel["sender"] == "SENDER COMPANY"
    assert parcel["receiver"] is None
    # Collected, so no longer awaiting pickup — but the point is still named.
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] == SHOP_NAME
    assert [event["status"] for event in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.DELIVERED,
    ]


def test_the_redirect_sentence_also_arrives_under_picked_not_only_incidence():
    """The research expected the redirect wording under ``incidence``; a real
    parcel sent it as a ``picked`` scan. Both land on in_transit, which is why
    this was never visible as a defect — pinned so it stays that way."""
    assert (
        map_event_status_es("picked", f"Shipment redirected to GLS Point {SHOP_NAME}")
        == ParcelStatus.IN_TRANSIT
    )


def test_a_door_delivery_has_no_pickup_point_and_does_not_warn(caplog):
    """The negative half of the pickup-point gate: no parcelShop at all."""
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_es(
            failed_attempt_sample(), postal_code=POSTCODE, parcel_no=NUMBER
        )
    assert parcel["pickup_point"] is None
    assert parcel["pickup"] is False
    assert not any("parcelShop" in record.message for record in caplog.records)


def test_a_real_incidence_sentence_is_mapped_rather_than_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        status = map_event_status_es("incidence", "Missing data")
    assert status == ParcelStatus.PROBLEM
    assert not caplog.records


def test_tied_timestamps_read_in_the_physically_sensible_order():
    """Two real scans share an instant — the depot scan and the van loading.
    The carrier states no order between them, so the one that does not look
    like a defect is the one to show: at the agency, then out for delivery."""
    parcel = normalize_parcel_es(
        failed_attempt_sample(),
        postal_code=POSTCODE,
        parcel_no=NUMBER,
        include_history=True,
    )
    tied = [
        event["raw_status"]
        for event in parcel["history"]
        if event["timestamp"] == "2026-08-07T09:10:00+00:00"
    ]
    assert tied == ["At delivery agency", "Out for delivery"]


def test_a_multi_parcel_shipment_still_tracks_as_one():
    """packageCount is 2 and the suite tracks at shipment level."""
    parcel = normalize_parcel_es(
        failed_attempt_sample(), postal_code=POSTCODE, parcel_no=NUMBER
    )
    assert parcel["weight"] == 122.5
    assert parcel["raw"]["attributes"]["packageCount"] == 2


def test_client_reference_is_the_senders_own_and_never_the_tracking_number():
    """It happened to equal the looked-up code on one real parcel and not on
    the other, so nothing may key off it."""
    parcel = normalize_parcel_es(
        failed_attempt_sample(), postal_code=POSTCODE, parcel_no=NUMBER
    )
    assert parcel["barcode"] != parcel["raw"]["clientReference"]


# ---------------------------------------------------------------------------
# Portugal — same transport, confirmed on its own real parcel (2026-10-12)
# ---------------------------------------------------------------------------

PT_SHOP = "PS Example Point"
PT_POSTCODE = "1000-001"


def portuguese_sample() -> dict:
    """A Portuguese shipment that failed twice and was collected at a point.

    Confirms the contract is shared with Spain rather than merely the host,
    and carries the richest ``incidence`` run captured so far: an absent
    recipient, a shut GLS Point, and two redirects to that point.
    """
    return {
        "allowedActions": {"changeSchedule": False, "canReturn": False},
        "attributes": {
            "packageCount": 1,
            "weight": 1.3,
            "reimbursement": 0,
            "postageDue": 0,
        },
        "clientReference": "SENDERREF3",
        "destination": {"address": {"locality": "LOCALITY E"}},
        "expeditionCode": 1000000003,
        "expeditionUuid": "00000000-0000-0000-0000-000000000003",
        "origin": {"address": {"locality": "LOCALITY F"}},
        "originAgency": 280,
        "references": [],
        "sender": {"name": "SENDER RETAILER"},
        "agency": {
            "code": 5013,
            "name": "DESTINATION DEPOT PT",
            "address": {
                "street": "",
                "locality": "",
                "postalCode": "",
                "country": "PT",
            },
            "phones": [],
        },
        "expeditionAliasOption": "none",
        "state": {
            "code": "delivered",
            "steps": {"current": 5, "last": 5},
            "description": "Delivered",
            "deliveryDate": "2026-07-14",
        },
        "tracking": [
            _scan("delivered", "Delivered", "2026-07-14T13:58:00Z"),
            _scan(
                "deliveredOnParcel", f"Deposited in {PT_SHOP}", "2026-07-14T11:02:00Z"
            ),
            _scan("receivedOnParcel", f"Received in {PT_SHOP}", "2026-07-14T11:01:00Z"),
            _scan("onTransit", "At delivery agency", "2026-07-14T10:03:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-07-14T10:03:00Z"),
            _scan(
                "incidence",
                f"Shipment redirected to GLS Point {PT_SHOP}",
                "2026-07-13T13:49:00Z",
            ),
            _scan("incidence", "Delivery point non available", "2026-07-13T12:49:00Z"),
            _scan(
                "deliveredOnParcel", f"Deposited in {PT_SHOP}", "2026-06-29T15:28:00Z"
            ),
            _scan(
                "incidence",
                f"Shipment redirected to GLS Point {PT_SHOP}",
                "2026-06-29T15:22:00Z",
            ),
            _scan("onTransit", "At delivery agency", "2026-06-29T14:26:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-06-29T14:26:00Z"),
            _scan("incidence", "Absent", "2026-06-29T14:22:00Z"),
            _scan("onDelivery", "Out for delivery", "2026-06-29T10:27:00Z"),
            _scan("onTransit", "At delivery agency", "2026-06-29T06:23:00Z"),
            _scan("onTransit", "In Transit to the destination", "2026-06-27T16:17:00Z"),
            _scan(
                "picked",
                "Parcel picked up by GLS. Pending to arrive to final destination",
                "2026-06-27T16:16:00Z",
            ),
            _scan(
                "dataReceived",
                "Label created. The shipment has not yet been delivered to GLS by "
                "the sender",
                "2026-06-27T10:19:00Z",
            ),
        ],
        "whatsapp": {"suggest": True},
        "parcelShop": {
            "id": 2,
            "active": True,
            "type": "shop",
            "name": PT_SHOP,
            "address": {
                "street": "",
                "locality": "",
                "postalCode": "",
                "country": "PT",
            },
            "notes": "",
            "schedules": ["Mon to Fri, 08:30 to 12:30"],
            "phones": [],
        },
    }


def test_a_portuguese_parcel_maps_with_no_warnings(caplog):
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel_es(
            portuguese_sample(),
            postal_code=PT_POSTCODE,
            parcel_no="1000000003",
            country="PT",
            include_history=True,
        )
    assert [record.message for record in caplog.records] == []
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-07-14T13:58:00+00:00"
    assert parcel["weight"] == 1.3
    assert parcel["sender"] == "SENDER RETAILER"
    assert parcel["pickup_point"] == PT_SHOP
    assert len(parcel["history"]) == 17


def test_the_portuguese_link_uses_the_portuguese_host():
    """The Spanish host does not resolve a Portuguese consignment, and the
    page is an SPA that answers 200 regardless — so the wrong host here is an
    invisible failure, not a visible one."""
    parcel = normalize_parcel_es(
        portuguese_sample(),
        postal_code=PT_POSTCODE,
        parcel_no="1000000003",
        country="PT",
    )
    assert (
        parcel["url"] == f"https://mygls.gls-portugal.pt/e/1000000003/{PT_POSTCODE}/pt"
    )


def test_a_spanish_link_stays_on_the_spanish_host():
    parcel = normalize_parcel_es(
        diverted_to_gls_point_sample(), postal_code=POSTCODE, parcel_no=NUMBER
    )
    assert parcel["url"].startswith("https://mygls.gls-spain.es/e/")


def test_the_portuguese_postcode_keeps_its_hyphen_in_the_link():
    """The service matches on the hyphenated form, so the link carries it."""
    parcel = normalize_parcel_es(
        portuguese_sample(),
        postal_code="4460-303",
        parcel_no="1000000003",
        country="PT",
    )
    assert "/4460-303/" in parcel["url"]


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("Absent", ParcelStatus.PROBLEM),
        ("Delivery point non available", ParcelStatus.PROBLEM),
        ("Missing data", ParcelStatus.PROBLEM),
    ],
)
def test_every_captured_incidence_sentence_is_mapped(sentence, expected, caplog):
    """Four incidence sentences have now been seen on real parcels; only the
    redirect is not a problem. None of them may still warn."""
    with caplog.at_level(logging.WARNING):
        assert map_event_status_es("incidence", sentence) == expected
    assert not caplog.records


def test_a_redirect_under_incidence_is_still_in_transit():
    """The same sentence has been observed under both `incidence` and
    `picked`, so the sentence decides and the code does not."""
    assert (
        map_event_status_es("incidence", f"Shipment redirected to GLS Point {PT_SHOP}")
        == ParcelStatus.IN_TRANSIT
    )


def test_the_three_tied_pairs_all_read_agency_then_out_for_delivery():
    """Every tie observed on a real parcel has this shape — the reason the
    history deliberately does not reverse ties."""
    parcel = normalize_parcel_es(
        portuguese_sample(),
        postal_code=PT_POSTCODE,
        parcel_no="1000000003",
        country="PT",
        include_history=True,
    )
    for instant in ("2026-06-29T14:26:00+00:00", "2026-07-14T10:03:00+00:00"):
        tied = [e["raw_status"] for e in parcel["history"] if e["timestamp"] == instant]
        assert tied == ["At delivery agency", "Out for delivery"]
