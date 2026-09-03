"""Tests for the GLS group-leaf transport, normalize_parcel_group and map_parcel_status_group.

The ``rstt028`` fixture is built from the real captured CZ body in
group-rest.md's "Payload — rstt028, CAPTURED 2026-08-23" section (the
``CUSTREF`` order reference is redacted there and stays redacted here). The
exact ``progressBar.statusText`` string for this consignment was never
transcribed in the mechanics doc (only "identical shape to rstt029's" is
recorded) — the fixture uses the top-level ``status: "Delivered"`` string
already captured as a reasonable stand-in; it is not itself wire-verified.

The bottom section covers AT/IE/FR/SI/HR/IT
(BUILD_PLAN_GROUP_COUNTRIES.md) — every one of those rows shipped past its
§0 gate, so their fixtures are honestly the CZ-shaped body wearing that
country's own host/locale/AWB, **not** a real capture — see each test's
docstring.
"""
from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import COUNTRIES, GlsApiError, ParcelStatus
from custom_components.gls.countries import group as group_module
from custom_components.gls.countries.group import (
    GlsGroupMaintenanceError,
    async_get_parcel_group,
    map_event_status_group,
    map_parcel_status_group,
    normalize_parcel_group,
)

AWB = "1234567890"
POSTAL_CODE = "11000"


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep countries/group's one-shot WARNING dedup state isolated per test."""
    def _reset():
        group_module._outage_warned = False
        group_module._postcode_mismatch_warned.clear()
        group_module._unmapped_status_logged.clear()
        group_module._unmapped_event_logged.clear()
        group_module._unexpected_keys_logged.clear()
        group_module._history_order_warned = False
        group_module._unparseable_timestamp_warned = False
        group_module._weight_format_logged.clear()
        group_module._unexpected_info_type_logged.clear()
        group_module._e800_warned.clear()
        group_module._type_retry_warned.clear()
        group_module._unexpected_5xx_warned = False

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Fixture — the captured rstt028 body (group-rest.md, 2026-08-23)
# ---------------------------------------------------------------------------


def _history_event(date: str, time: str, evt_no: str, evt_dscr: str) -> dict:
    return {"date": date, "time": time, "evtNo": evt_no, "evtDscr": evt_dscr}


def captured_history_newest_first() -> list[dict]:
    """The eight real (redacted-free — no PII in this payload) history events."""
    return [
        _history_event("2026-06-25", "13:38:31", "3.0", "The parcel has been delivered."),
        _history_event(
            "2026-06-25",
            "13:19:02",
            "3.896",
            "The parcel has been delivered into the ParcelLocker.",
        ),
        _history_event(
            "2026-06-25",
            "13:18:56",
            "3.896",
            "The parcel has been delivered into the ParcelLocker.",
        ),
        _history_event(
            "2026-06-25",
            "07:20:06",
            "11.0",
            "The parcel is expected to be delivered during the day.",
        ),
        _history_event(
            "2026-06-25", "05:43:22", "2.0", "The parcel has reached the parcel center."
        ),
        _history_event(
            "2026-06-24", "21:23:15", "2.0", "The parcel has reached the parcel center."
        ),
        _history_event("2026-06-24", "12:49:01", "0.0", "The parcel was handed over to GLS."),
        _history_event(
            "2026-06-24",
            "10:39:34",
            "0.100",
            "The parcel data was entered into the GLS IT system; the parcel "
            "was not yet handed over to GLS.",
        ),
    ]


def captured_sample_cz(*, status_info: str = "DELIVERED", retour_flag: bool = False) -> dict:
    """A trimmed, real (CUSTREF-redacted) captured GLS-CZ ``rstt028`` body."""
    return {
        "tuNo": AWB,
        "referenceNo": AWB,
        "date": "2026-06-24",  # consignment date — NOT delivered_at
        "postalCode": POSTAL_CODE,
        "status": "Delivered",
        "natSysOwnerCode": "CZ02",
        "deliveryOwnerCode": "CZ02",
        "owners": [{"code": "CZ02", "type": "DELIVERY"}],
        "emailNotificationCard": False,
        "signature": {"validate": True, "name": "Signature:", "value": "true"},
        "infos": [{"type": "WEIGHT", "name": "Weight:", "value": "0.9 kg"}],
        "references": [
            {"type": "UNITNO", "name": "Parcel number:", "value": AWB},
            {"type": "CUSTREF", "name": "Reference no:", "value": "<REDACTED>"},
        ],
        "progressBar": {
            "level": 100,
            "statusInfo": status_info,
            "statusText": "Delivered",
            "retourFlag": retour_flag,
            "colourIndex": 4,
            "evtNos": ["3.0", "3.896", "11.0", "2.0", "0.0", "0.100"],
            "statusBar": [
                {"status": "PREADVICE", "imageStatus": "COMPLETE", "imageText": "Information", "statusText": ""},
                {"status": "INTRANSIT", "imageStatus": "COMPLETE", "imageText": "In transit", "statusText": ""},
                {"status": "INWAREHOUSE", "imageStatus": "COMPLETE", "imageText": "Depot", "statusText": ""},
                {"status": "INDELIVERY", "imageStatus": "COMPLETE", "imageText": "Out for delivery", "statusText": ""},
                {"status": "DELIVERED", "imageStatus": "CURRENT", "imageText": "Delivered", "statusText": "Delivered."},
            ],
        },
        "history": captured_history_newest_first(),
    }


# ---------------------------------------------------------------------------
# map_parcel_status_group — own function, exact-match table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PREADVICE", ParcelStatus.REGISTERED),
        ("PROCESSING", ParcelStatus.REGISTERED),
        ("INTRANSIT", ParcelStatus.IN_TRANSIT),
        ("INWAREHOUSE", ParcelStatus.IN_TRANSIT),
        ("INPICKUP", ParcelStatus.IN_TRANSIT),
        ("MULTIPACK", ParcelStatus.IN_TRANSIT),
        ("INDELIVERY", ParcelStatus.OUT_FOR_DELIVERY),
        ("DELIVEREDPS", ParcelStatus.AT_PICKUP_POINT),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("NOTPICKEDUP", ParcelStatus.RETURNING),
        ("RETURNED", ParcelStatus.RETURNING),
        ("NOTDELIVERED", ParcelStatus.PROBLEM),
        ("CANCELLED", ParcelStatus.PROBLEM),
        ("CANCELED", ParcelStatus.PROBLEM),
        ("UNAVAILABLE", ParcelStatus.PROBLEM),
    ],
)
def test_map_parcel_status_known(value, expected):
    assert map_parcel_status_group(value) == expected


def test_deliveredps_is_not_delivered():
    """The band-ordering trap the plan names explicitly: DELIVEREDPS contains
    the substring DELIVERED — an in/startswith check would misreport it."""
    assert map_parcel_status_group("DELIVEREDPS") != ParcelStatus.DELIVERED
    assert map_parcel_status_group("DELIVEREDPS") == ParcelStatus.AT_PICKUP_POINT


def test_none_status_info_is_unknown():
    assert map_parcel_status_group(None) == ParcelStatus.UNKNOWN


def test_unmapped_status_is_unknown_and_warns_once(caplog):
    with caplog.at_level("WARNING"):
        first = map_parcel_status_group("SOMETHING_NEW")
        second = map_parcel_status_group("SOMETHING_NEW")
    assert first == ParcelStatus.UNKNOWN
    assert second == ParcelStatus.UNKNOWN
    warnings = [m for m in caplog.messages if "SOMETHING_NEW" in m]
    assert len(warnings) == 1


def test_retour_flag_overrides_delivered():
    assert map_parcel_status_group("DELIVERED", retour_flag=True) == ParcelStatus.RETURNING


def test_retour_flag_overrides_unmapped_without_warning(caplog):
    with caplog.at_level("WARNING"):
        status = map_parcel_status_group("ANYTHING", retour_flag=True)
    assert status == ParcelStatus.RETURNING
    assert not any("ANYTHING" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# map_event_status_group — per history[] entry, from evtNo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evt_no", "expected"),
    [
        ("0.100", ParcelStatus.REGISTERED),
        ("0.0", ParcelStatus.IN_TRANSIT),
        ("2.0", ParcelStatus.IN_TRANSIT),
        ("17.0", ParcelStatus.IN_TRANSIT),
        ("11.0", ParcelStatus.OUT_FOR_DELIVERY),
        ("3.0", ParcelStatus.DELIVERED),
        ("3.896", ParcelStatus.AT_PICKUP_POINT),
    ],
)
def test_map_event_status_known(evt_no, expected):
    assert map_event_status_group(evt_no) == expected


def test_map_event_status_none_input_is_none_without_warning(caplog):
    with caplog.at_level("WARNING"):
        assert map_event_status_group(None) is None
    assert not caplog.messages


def test_map_event_status_unmapped_is_none_and_warns_once(caplog):
    with caplog.at_level("WARNING"):
        first = map_event_status_group("9.999")
        second = map_event_status_group("9.999")
    assert first is None
    assert second is None
    warnings = [m for m in caplog.messages if "9.999" in m]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# normalize_parcel_group
# ---------------------------------------------------------------------------


def test_normalize_captured_delivered_parcel():
    parcel = normalize_parcel_group(captured_sample_cz(), parcel_no=AWB)
    assert parcel["carrier"] == "GLS"
    assert parcel["barcode"] == AWB
    assert parcel["status"] == ParcelStatus.DELIVERED
    # Newest history entry's evtDscr, not progressBar.statusText's heading
    # ("Delivered") — ha-gls#6.
    assert parcel["raw_status"] == "The parcel has been delivered."
    assert parcel["delivered"] is True
    # Newest history entry, NOT the top-level "date" (consignment date trap).
    assert parcel["delivered_at"] == "2026-06-25T13:38:31"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["weight"] == 0.9
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off
    assert parcel["url"] == f"https://gls-group.eu/CZ/en/parcel-tracking?match={AWB}"
    assert parcel["raw"]["tuNo"] == AWB


def test_normalize_barcode_prefers_supplied_parcel_no_over_tu_no():
    """tuNo happened to equal the AWB for this capture, but must never be
    trusted — mutate it away from the AWB and confirm barcode still comes
    from the caller-supplied parcel_no."""
    raw = captured_sample_cz()
    raw["tuNo"] = "SOME-OTHER-ID"
    raw["referenceNo"] = "SOME-OTHER-ID"
    parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["barcode"] == AWB


def test_normalize_barcode_falls_back_without_a_supplied_parcel_no():
    parcel = normalize_parcel_group(captured_sample_cz())
    assert parcel["barcode"] == AWB  # referenceNo/tuNo fallback


def test_normalize_history_opt_in_is_reversed_to_oldest_first():
    parcel = normalize_parcel_group(captured_sample_cz(), parcel_no=AWB, include_history=True)
    history = parcel["history"]
    assert len(history) == 8
    assert history[0]["timestamp"] == "2026-06-24T10:39:34"
    assert history[-1]["timestamp"] == "2026-06-25T13:38:31"
    assert "entered into the GLS IT system" in history[0]["raw_status"]
    assert history[-1]["raw_status"] == "The parcel has been delivered."
    # Per-event status, mapped from evtNo (oldest -> newest, matching the
    # reversed history): registered, handed over, twice at a parcel center,
    # out for delivery, the locker deposit (x2), delivered.
    assert [event["status"] for event in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.DELIVERED,
    ]


def test_normalize_history_does_not_deduplicate_repeated_evtno():
    """evtNo 3.896 and 2.0 both repeat in the capture — every entry stays."""
    parcel = normalize_parcel_group(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert len(parcel["history"]) == 8


def test_normalize_pickup_at_pickup_point():
    parcel = normalize_parcel_group(
        captured_sample_cz(status_info="DELIVEREDPS"), parcel_no=AWB
    )
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None  # only computed when delivered
    assert parcel["pickup_point"] is None  # never structured
    # progressBar.statusText stays "Delivered" (the progress bar's own
    # heading) regardless of status_info override — raw_status must come
    # from the newest history event instead, never that heading (ha-gls#6).
    assert parcel["raw_status"] != "Delivered"


def test_normalize_retour_flag_overrides_status():
    parcel = normalize_parcel_group(
        captured_sample_cz(status_info="DELIVERED", retour_flag=True), parcel_no=AWB
    )
    assert parcel["status"] == ParcelStatus.RETURNING
    # retourFlag overriding the *status* must not fabricate a delivered_at —
    # "delivered" itself is still driven by the exact statusInfo match.
    assert parcel["delivered"] is True


def test_normalize_weight_parses_unit_suffixed_string():
    parcel = normalize_parcel_group(captured_sample_cz(), parcel_no=AWB)
    assert parcel["weight"] == 0.9


def test_normalize_weight_missing_infos_is_none():
    raw = captured_sample_cz()
    raw["infos"] = []
    parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["weight"] is None


def test_normalize_no_history_at_all():
    raw = captured_sample_cz()
    raw["history"] = []
    parcel = normalize_parcel_group(raw, parcel_no=AWB, include_history=True)
    assert parcel["history"] == []
    assert parcel["delivered_at"] is None


def test_normalize_url_uses_barcode_not_tu_no():
    raw = captured_sample_cz()
    raw["tuNo"] = "SOME-OTHER-ID"
    parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["url"] == f"https://gls-group.eu/CZ/en/parcel-tracking?match={AWB}"


def test_normalize_url_none_without_a_barcode():
    raw = captured_sample_cz()
    del raw["tuNo"]
    del raw["referenceNo"]
    parcel = normalize_parcel_group(raw)  # no parcel_no supplied either
    assert parcel["barcode"] is None
    assert parcel["url"] is None


# ---------------------------------------------------------------------------
# §8 WARNING obligations (already-shipped CZ ones)
# ---------------------------------------------------------------------------


def test_unexpected_top_level_key_is_warned_once(caplog):
    raw = captured_sample_cz()
    raw["someNewField"] = 123
    with caplog.at_level("WARNING"):
        normalize_parcel_group(raw, parcel_no=AWB)
        normalize_parcel_group(raw, parcel_no=AWB)
    key_warnings = [m for m in caplog.messages if "someNewField" in m]
    assert len(key_warnings) == 1
    assert "someNewField: int" in key_warnings[0]


def test_known_keys_do_not_warn(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_group(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert not any("unrecognised top-level" in m.lower() for m in caplog.messages)


def test_history_order_violation_warns_once(caplog):
    raw = captured_sample_cz()
    # Swap two entries so the list is no longer strictly newest-first.
    raw["history"][0], raw["history"][1] = raw["history"][1], raw["history"][0]
    with caplog.at_level("WARNING"):
        normalize_parcel_group(raw, parcel_no=AWB, include_history=True)
        normalize_parcel_group(raw, parcel_no=AWB, include_history=True)
    warnings = [m for m in caplog.messages if "not newest-first" in m]
    assert len(warnings) == 1


def test_correctly_ordered_history_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_group(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert not any("not newest-first" in m for m in caplog.messages)


def test_unparseable_timestamp_warns_once(caplog):
    raw = captured_sample_cz()
    raw["history"][0]["date"] = "not-a-date"
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_group(raw, parcel_no=AWB, include_history=True)
    assert parcel["delivered_at"] is None  # the newest entry failed to parse
    assert any("could not be parsed" in m for m in caplog.messages)


def test_yy_mm_dd_date_variant_is_accepted():
    raw = captured_sample_cz()
    raw["history"][0]["date"] = "26-06-25"  # YY-MM-DD, per group-rest.md's caution
    parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["delivered_at"] == "2026-06-25T13:38:31"


def test_history_event_missing_time_has_no_timestamp():
    raw = captured_sample_cz()
    del raw["history"][0]["time"]
    parcel = normalize_parcel_group(raw, parcel_no=AWB, include_history=True)
    assert parcel["delivered_at"] is None
    assert parcel["history"][-1]["timestamp"] is None


def test_weight_wrong_unit_warns_and_is_none(caplog):
    raw = captured_sample_cz()
    raw["infos"] = [{"type": "WEIGHT", "name": "Weight:", "value": "0.9 lb"}]
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["weight"] is None
    assert any("unexpected format" in m for m in caplog.messages)


def test_weight_unparseable_value_warns_and_is_none(caplog):
    raw = captured_sample_cz()
    raw["infos"] = [{"type": "WEIGHT", "name": "Weight:", "value": "heavy"}]
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["weight"] is None
    assert any("unexpected format" in m for m in caplog.messages)


def test_infos_unexpected_type_warns_once(caplog):
    raw = captured_sample_cz()
    raw["infos"].append({"type": "DIMENSIONS", "name": "Size:", "value": "30x20x10 cm"})
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_group(raw, parcel_no=AWB)
        normalize_parcel_group(raw, parcel_no=AWB)
    assert parcel["dimensions"] is None  # never auto-mapped, even if seen
    warnings = [m for m in caplog.messages if "'DIMENSIONS'" in m]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Transport — async_get_parcel_group
# ---------------------------------------------------------------------------


def _get_ctx(status: int, text: str = "") -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_with(*responses: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


HOST = COUNTRIES["CZ"]["host"]
GROUP_LOCALE = COUNTRIES["CZ"]["group_locale"]


async def test_transport_returns_body_on_200():
    session = _session_with(_get_ctx(200, _json.dumps(captured_sample_cz())))
    body = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert body["tuNo"] == AWB
    url = session.get.call_args[0][0]
    assert HOST in url
    assert "rstt028" in url
    assert AWB in url
    assert POSTAL_CODE in url


async def test_transport_strips_whitespace_from_postal_code():
    session = _session_with(_get_ctx(200, _json.dumps(captured_sample_cz())))
    await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "110 00")
    url = session.get.call_args[0][0]
    assert "postalCode=11000" in url


async def test_transport_e800_returns_none_and_warns_once(caplog):
    """E800 usually means absence — it stays a semantic "not found" — but
    now fires a one-shot WARNING per parcel (BUILD_PLAN_GROUP_COUNTRIES.md
    §6/§8), since one Spanish code proved E800 isn't always absence."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E800","exceptionText":"x"}'),
        _get_ctx(404, '{"lastError":"E800","exceptionText":"x"}'),
    )
    with caplog.at_level("WARNING"):
        first = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
        second = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert first is None
    assert second is None
    warnings = [m for m in caplog.messages if "does not carry parcel" in m]
    assert len(warnings) == 1


async def test_transport_e206_returns_none_without_e800_warning(caplog):
    session = _session_with(_get_ctx(404, '{"lastError":"E206","exceptionText":"x"}'))
    with caplog.at_level("WARNING"):
        result = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert result is None
    assert not any("does not carry parcel" in m for m in caplog.messages)


async def test_transport_e609_falls_back_to_rstt029(caplog):
    fallback_body = {
        "tuStatus": [
            {
                "tuNo": AWB,
                "postalCode": "",
                "owners": [],
                "progressBar": captured_sample_cz()["progressBar"],
            }
        ]
    }
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609","exceptionText":"x"}'),
        _get_ctx(200, _json.dumps(fallback_body)),
    )
    with caplog.at_level("WARNING"):
        body = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert body["tuNo"] == AWB
    assert "history" not in body  # the fallback never carries it
    second_url = session.get.call_args_list[1][0][0]
    assert "rstt029" in second_url
    assert AWB in second_url
    assert any("postal code" in m.lower() for m in caplog.messages)


async def test_transport_e609_postcode_warning_fires_once_per_parcel(caplog):
    fallback_body = {"tuStatus": [{"tuNo": AWB, "progressBar": {}}]}
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, _json.dumps(fallback_body)),
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, _json.dumps(fallback_body)),
    )
    with caplog.at_level("WARNING"):
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    mismatch_warnings = [m for m in caplog.messages if "does not match" in m]
    assert len(mismatch_warnings) == 1


async def test_transport_e609_fallback_with_no_tu_status_returns_none():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, '{"tuStatus": []}'),
    )
    result = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert result is None


async def test_transport_fallback_semantic_error_returns_none():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(404, '{"lastError":"E000"}'),
    )
    result = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert result is None


async def test_transport_maintenance_page_raises_and_warns_once(caplog):
    session = _session_with(
        _get_ctx(200, "<html>Maintenance</html>"),
        _get_ctx(200, "<html>Maintenance</html>"),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsGroupMaintenanceError):
            await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
        with pytest.raises(GlsGroupMaintenanceError):
            await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert issubclass(GlsGroupMaintenanceError, GlsApiError)
    outage_warnings = [m for m in caplog.messages if "down" in m.lower()]
    assert len(outage_warnings) == 1


async def test_transport_maintenance_page_on_fallback_also_raises():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, "<html>Maintenance</html>"),
    )
    with pytest.raises(GlsGroupMaintenanceError):
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")


async def test_transport_raises_gls_api_error_on_unexpected_status():
    session = _session_with(_get_ctx(500, ""))
    with pytest.raises(GlsApiError):
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)


async def test_transport_raises_on_malformed_postcode_field_exception():
    """A 400 with a valid JSON body but no ``lastError`` (a malformed-postcode
    field exception, group-rest.md) is still a hard error, not a semantic miss."""
    body = (
        '{"fieldExceptions":[{"attribute":"zipcode","exceptionText":"x"}],'
        '"exceptionText":"x"}'
    )
    session = _session_with(_get_ctx(400, body))
    with pytest.raises(GlsApiError):
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)


async def test_transport_empty_body_is_treated_as_outage():
    session = _session_with(_get_ctx(200, ""))
    with pytest.raises(GlsGroupMaintenanceError):
        await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)


async def test_transport_primary_5xx_with_json_body_warns_once_and_raises(caplog):
    """A 5xx on the *primary* rstt028 call (JSON body, no lastError) must not
    be read as "not found" — it's the same class of fault as Italy's
    rstt029 500 (BUILD_PLAN_GROUP_COUNTRIES.md §3), so it also fires the
    one-shot 5xx WARNING and still raises GlsApiError so the coordinator's
    transient handling applies."""
    session = _session_with(
        _get_ctx(503, "{}"),
        _get_ctx(503, "{}"),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsApiError):
            await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
        with pytest.raises(GlsApiError):
            await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    warnings = [m for m in caplog.messages if "unexpected server error" in m]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# §3 — the type=/type=NAT retry on the rstt029 fallback
# ---------------------------------------------------------------------------


def _fallback_ok_body(tu_no: str = AWB) -> str:
    return _json.dumps(
        {"tuStatus": [{"tuNo": tu_no, "progressBar": captured_sample_cz()["progressBar"]}]}
    )


async def test_type_retry_preferred_first_hit_no_warning(caplog):
    """Italy prefers type=NAT — when that first attempt already succeeds,
    there is no retry and no WARNING."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),  # primary -> fallback
        _get_ctx(200, _fallback_ok_body()),  # type=NAT hits immediately
    )
    with caplog.at_level("WARNING"):
        body = await async_get_parcel_group(
            session, HOST, "IT/en", AWB, "00000", country="IT"
        )
    assert body["tuNo"] == AWB
    first_type_url = session.get.call_args_list[1][0][0]
    assert "type=NAT" in first_type_url
    assert not any("different lookup type" in m for m in caplog.messages)


async def test_type_retry_fallback_hit_after_500_warns_once(caplog):
    """type= 500s, type=NAT resolves it — the exact Italian M-prefixed-AWB
    shape from group-rest.md's sweep. Fires the one-shot type-retry WARNING."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),  # primary -> fallback
        _get_ctx(500, '{"exceptionText":"Errore di sistema"}'),  # type= fails
        _get_ctx(200, _fallback_ok_body("M4 663093258")),  # type=NAT hits
    )
    with caplog.at_level("WARNING"):
        body = await async_get_parcel_group(
            session, HOST, "IT/en", "M4663093258", "00000", country="IT_PLAIN"
        )
    assert body["tuNo"] == "M4 663093258"
    urls = [call[0][0] for call in session.get.call_args_list]
    assert "type=" in urls[1] and "type=NAT" not in urls[1]
    assert "type=NAT" in urls[2]
    assert any("different lookup type" in m for m in caplog.messages)
    assert any("unexpected server error" in m for m in caplog.messages)


async def test_type_retry_fallback_hit_after_e206_warns_and_no_5xx_warning(caplog):
    """A 404 E206 (not a 5xx) also triggers the retry, but must not fire the
    5xx WARNING — only an actual server fault does."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(404, '{"lastError":"E206"}'),
        _get_ctx(200, _fallback_ok_body()),
    )
    with caplog.at_level("WARNING"):
        body = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert body["tuNo"] == AWB
    assert any("different lookup type" in m for m in caplog.messages)
    assert not any("unexpected server error" in m for m in caplog.messages)


async def test_type_retry_both_fail_5xx_raises_gls_api_error(caplog):
    """Both type= and type=NAT fault with a 5xx — a server problem, not a
    "not found", so it must raise (the coordinator keeps cached data and
    retries) rather than silently returning None."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(500, '{"exceptionText":"x"}'),
        _get_ctx(502, '{"exceptionText":"x"}'),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsApiError):
            await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    warnings = [m for m in caplog.messages if "unexpected server error" in m]
    assert len(warnings) == 1  # one-shot even though both attempts 5xx'd


async def test_type_retry_both_fail_e206_returns_none():
    """Both type= and type=NAT come back structurally rejected (E206) — a
    legitimate "not found", so this stays a silent None, not an error."""
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(404, '{"lastError":"E206"}'),
        _get_ctx(404, '{"lastError":"E206"}'),
    )
    result = await async_get_parcel_group(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert result is None


# ---------------------------------------------------------------------------
# The Group-leaf countries added past their §0 gate
# (BUILD_PLAN_GROUP_COUNTRIES.md). Every fixture below is synthetic: it uses
# the country-specific host/locale and a fake AWB, while the response remains
# the CZ-shaped rstt028 stand-in described below.
# ---------------------------------------------------------------------------


def _group_shaped_fixture(country: str, awb: str, *, status_info: str = "DELIVERED") -> dict:
    """A CZ-shaped rstt028 body for ``country``/``awb`` — NOT a real capture.

    group-rest.md's sweep found no shape difference across every country
    that answered rstt029, and every one of them read statusInfo:
    DELIVERED, so this is the most honest stand-in available for a country
    whose rstt028 has never actually returned 200 (BUILD_PLAN_GROUP_COUNTRIES.md
    §0/§10).
    """
    raw = captured_sample_cz(status_info=status_info)
    raw["tuNo"] = awb
    raw["referenceNo"] = awb
    raw["natSysOwnerCode"] = f"{country}01"
    raw["deliveryOwnerCode"] = f"{country}01"
    raw["owners"] = [{"code": f"{country}01", "type": "DELIVERY"}]
    raw["references"][0]["value"] = awb
    return raw


@pytest.mark.parametrize(
    ("country", "awb", "postal_code"),
    [
        ("AT", "ATTEST00001", "1010"),
        ("SK", "SKTEST00001", "82101"),
        ("IE", "IETEST00001", "D02AF30"),
        ("FR", "FRTEST00001", "39100"),
        ("SI", "SITEST00001", "1000"),
        ("HR", "HRTEST00001", "10000"),
    ],
)
async def test_group_leaf_country_end_to_end(country, awb, postal_code):
    """Exercise a synthetic parcel through each country's group-leaf wiring.

    The fixture is CZ-shaped because the detail leaf is not captured for every
    country. The AWBs are deliberately non-live test values; this test covers
    country-specific host, locale and tracking-link dispatch only.
    """
    host = COUNTRIES[country]["host"]
    group_locale = COUNTRIES[country]["group_locale"]
    fixture = _group_shaped_fixture(country, awb)

    session = _session_with(_get_ctx(200, _json.dumps(fixture)))
    body = await async_get_parcel_group(
        session, host, group_locale, awb, postal_code, country=country
    )
    assert body["tuNo"] == awb
    url = session.get.call_args[0][0]
    assert group_locale in url
    assert awb in url

    parcel = normalize_parcel_group(body, country=country, parcel_no=awb)
    assert parcel["barcode"] == awb
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["url"] == COUNTRIES[country]["tracking_url"].format(parcel_no=awb)


async def test_italy_group_leaf_end_to_end_needs_type_nat():
    """Italy's own real AWB only ever resolved with type=NAT (an HTTP 500 on
    the empty value) — COUNTRIES["IT"]["group_type"] must make that the
    *first* attempt, so this only costs one HTTP call on the fallback leaf,
    not two."""
    awb = "M4663093258"
    fixture = _group_shaped_fixture("IT", "M4 663093258")  # GLS inserted a space
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),  # primary -> fallback
        _get_ctx(200, _json.dumps({"tuStatus": [fixture]})),  # type=NAT, first try
    )
    body = await async_get_parcel_group(
        session, COUNTRIES["IT"]["host"], COUNTRIES["IT"]["group_locale"],
        awb, "20121", country="IT",
    )
    assert body["tuNo"] == "M4 663093258"
    assert len(session.get.call_args_list) == 2  # no wasted second attempt
    fallback_url = session.get.call_args_list[1][0][0]
    assert "type=NAT" in fallback_url

    parcel = normalize_parcel_group(body, country="IT", parcel_no=awb)
    # tuNo carries GLS's inserted space; barcode must still be what the user typed.
    assert parcel["barcode"] == awb
    assert parcel["url"] == COUNTRIES["IT"]["tracking_url"].format(parcel_no=awb)
