"""Tests for GLS Czech Republic transport, normalize_parcel_cz and map_parcel_status_cz.

The ``rstt028`` fixture is built from the real captured body in
group-rest.md's "Payload — rstt028, CAPTURED 2026-08-23" section (the
``CUSTREF`` order reference is redacted there and stays redacted here). The
exact ``progressBar.statusText`` string for this consignment was never
transcribed in the mechanics doc (only "identical shape to rstt029's" is
recorded) — the fixture uses the top-level ``status: "Delivered"`` string
already captured as a reasonable stand-in; it is not itself wire-verified.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.gls.const import COUNTRIES, GlsApiError, ParcelStatus
from custom_components.gls.countries import cz as cz_module
from custom_components.gls.countries.cz import (
    GlsGroupMaintenanceError,
    async_get_parcel_cz,
    map_parcel_status_cz,
    normalize_parcel_cz,
)

AWB = "5036234901"
POSTAL_CODE = "25401"


@pytest.fixture(autouse=True)
def _reset_one_shot_state():
    """Keep countries/cz's one-shot WARNING dedup state isolated per test."""
    cz_module._outage_warned = False
    cz_module._postcode_mismatch_warned.clear()
    cz_module._unmapped_status_logged.clear()
    cz_module._unexpected_keys_logged.clear()
    cz_module._history_order_warned = False
    cz_module._unparseable_timestamp_warned = False
    cz_module._weight_format_logged.clear()
    cz_module._unexpected_info_type_logged.clear()
    yield
    cz_module._outage_warned = False
    cz_module._postcode_mismatch_warned.clear()
    cz_module._unmapped_status_logged.clear()
    cz_module._unexpected_keys_logged.clear()
    cz_module._history_order_warned = False
    cz_module._unparseable_timestamp_warned = False
    cz_module._weight_format_logged.clear()
    cz_module._unexpected_info_type_logged.clear()


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
# map_parcel_status_cz — own function, exact-match table (§4)
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
    assert map_parcel_status_cz(value) == expected


def test_deliveredps_is_not_delivered():
    """The band-ordering trap the plan names explicitly: DELIVEREDPS contains
    the substring DELIVERED — an in/startswith check would misreport it."""
    assert map_parcel_status_cz("DELIVEREDPS") != ParcelStatus.DELIVERED
    assert map_parcel_status_cz("DELIVEREDPS") == ParcelStatus.AT_PICKUP_POINT


def test_none_status_info_is_unknown():
    assert map_parcel_status_cz(None) == ParcelStatus.UNKNOWN


def test_unmapped_status_is_unknown_and_warns_once(caplog):
    with caplog.at_level("WARNING"):
        first = map_parcel_status_cz("SOMETHING_NEW")
        second = map_parcel_status_cz("SOMETHING_NEW")
    assert first == ParcelStatus.UNKNOWN
    assert second == ParcelStatus.UNKNOWN
    warnings = [m for m in caplog.messages if "SOMETHING_NEW" in m]
    assert len(warnings) == 1


def test_retour_flag_overrides_delivered():
    assert map_parcel_status_cz("DELIVERED", retour_flag=True) == ParcelStatus.RETURNING


def test_retour_flag_overrides_unmapped_without_warning(caplog):
    with caplog.at_level("WARNING"):
        status = map_parcel_status_cz("ANYTHING", retour_flag=True)
    assert status == ParcelStatus.RETURNING
    assert not any("ANYTHING" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# normalize_parcel_cz
# ---------------------------------------------------------------------------


def test_normalize_captured_delivered_parcel():
    parcel = normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB)
    assert parcel["carrier"] == "GLS"
    assert parcel["barcode"] == AWB
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
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
    parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["barcode"] == AWB


def test_normalize_barcode_falls_back_without_a_supplied_parcel_no():
    parcel = normalize_parcel_cz(captured_sample_cz())
    assert parcel["barcode"] == AWB  # referenceNo/tuNo fallback


def test_normalize_history_opt_in_is_reversed_to_oldest_first():
    parcel = normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB, include_history=True)
    history = parcel["history"]
    assert len(history) == 8
    assert history[0]["timestamp"] == "2026-06-24T10:39:34"
    assert history[-1]["timestamp"] == "2026-06-25T13:38:31"
    assert "entered into the GLS IT system" in history[0]["raw_status"]
    assert history[-1]["raw_status"] == "The parcel has been delivered."
    # §4: never derive a per-event status from evtNo.
    assert all(event["status"] is None for event in history)


def test_normalize_history_does_not_deduplicate_repeated_evtno():
    """evtNo 3.896 and 2.0 both repeat in the capture — every entry stays."""
    parcel = normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert len(parcel["history"]) == 8


def test_normalize_pickup_at_pickup_point():
    parcel = normalize_parcel_cz(
        captured_sample_cz(status_info="DELIVEREDPS"), parcel_no=AWB
    )
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None  # only computed when delivered
    assert parcel["pickup_point"] is None  # never structured (§3)


def test_normalize_retour_flag_overrides_status():
    parcel = normalize_parcel_cz(
        captured_sample_cz(status_info="DELIVERED", retour_flag=True), parcel_no=AWB
    )
    assert parcel["status"] == ParcelStatus.RETURNING
    # retourFlag overriding the *status* must not fabricate a delivered_at —
    # "delivered" itself is still driven by the exact statusInfo match.
    assert parcel["delivered"] is True


def test_normalize_weight_parses_unit_suffixed_string():
    parcel = normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB)
    assert parcel["weight"] == 0.9


def test_normalize_weight_missing_infos_is_none():
    raw = captured_sample_cz()
    raw["infos"] = []
    parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["weight"] is None


def test_normalize_no_history_at_all():
    raw = captured_sample_cz()
    raw["history"] = []
    parcel = normalize_parcel_cz(raw, parcel_no=AWB, include_history=True)
    assert parcel["history"] == []
    assert parcel["delivered_at"] is None


def test_normalize_url_uses_barcode_not_tu_no():
    raw = captured_sample_cz()
    raw["tuNo"] = "SOME-OTHER-ID"
    parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["url"] == f"https://gls-group.eu/CZ/en/parcel-tracking?match={AWB}"


def test_normalize_url_none_without_a_barcode():
    raw = captured_sample_cz()
    del raw["tuNo"]
    del raw["referenceNo"]
    parcel = normalize_parcel_cz(raw)  # no parcel_no supplied either
    assert parcel["barcode"] is None
    assert parcel["url"] is None


# ---------------------------------------------------------------------------
# §8 WARNING obligations
# ---------------------------------------------------------------------------


def test_unexpected_top_level_key_is_warned_once(caplog):
    raw = captured_sample_cz()
    raw["someNewField"] = 123
    with caplog.at_level("WARNING"):
        normalize_parcel_cz(raw, parcel_no=AWB)
        normalize_parcel_cz(raw, parcel_no=AWB)
    key_warnings = [m for m in caplog.messages if "someNewField" in m]
    assert len(key_warnings) == 1
    assert "someNewField: int" in key_warnings[0]


def test_known_keys_do_not_warn(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert not any("unrecognised top-level" in m.lower() for m in caplog.messages)


def test_history_order_violation_warns_once(caplog):
    raw = captured_sample_cz()
    # Swap two entries so the list is no longer strictly newest-first.
    raw["history"][0], raw["history"][1] = raw["history"][1], raw["history"][0]
    with caplog.at_level("WARNING"):
        normalize_parcel_cz(raw, parcel_no=AWB, include_history=True)
        normalize_parcel_cz(raw, parcel_no=AWB, include_history=True)
    warnings = [m for m in caplog.messages if "not newest-first" in m]
    assert len(warnings) == 1


def test_correctly_ordered_history_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        normalize_parcel_cz(captured_sample_cz(), parcel_no=AWB, include_history=True)
    assert not any("not newest-first" in m for m in caplog.messages)


def test_unparseable_timestamp_warns_once(caplog):
    raw = captured_sample_cz()
    raw["history"][0]["date"] = "not-a-date"
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_cz(raw, parcel_no=AWB, include_history=True)
    assert parcel["delivered_at"] is None  # the newest entry failed to parse
    assert any("could not be parsed" in m for m in caplog.messages)


def test_yy_mm_dd_date_variant_is_accepted():
    raw = captured_sample_cz()
    raw["history"][0]["date"] = "26-06-25"  # YY-MM-DD, per group-rest.md's caution
    parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["delivered_at"] == "2026-06-25T13:38:31"


def test_history_event_missing_time_has_no_timestamp():
    raw = captured_sample_cz()
    del raw["history"][0]["time"]
    parcel = normalize_parcel_cz(raw, parcel_no=AWB, include_history=True)
    assert parcel["delivered_at"] is None
    assert parcel["history"][-1]["timestamp"] is None


def test_weight_wrong_unit_warns_and_is_none(caplog):
    raw = captured_sample_cz()
    raw["infos"] = [{"type": "WEIGHT", "name": "Weight:", "value": "0.9 lb"}]
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["weight"] is None
    assert any("unexpected format" in m for m in caplog.messages)


def test_weight_unparseable_value_warns_and_is_none(caplog):
    raw = captured_sample_cz()
    raw["infos"] = [{"type": "WEIGHT", "name": "Weight:", "value": "heavy"}]
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["weight"] is None
    assert any("unexpected format" in m for m in caplog.messages)


def test_infos_unexpected_type_warns_once(caplog):
    raw = captured_sample_cz()
    raw["infos"].append({"type": "DIMENSIONS", "name": "Size:", "value": "30x20x10 cm"})
    with caplog.at_level("WARNING"):
        parcel = normalize_parcel_cz(raw, parcel_no=AWB)
        normalize_parcel_cz(raw, parcel_no=AWB)
    assert parcel["dimensions"] is None  # never auto-mapped, even if seen
    warnings = [m for m in caplog.messages if "'DIMENSIONS'" in m]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Transport — async_get_parcel_cz
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
    import json as _json

    session = _session_with(_get_ctx(200, _json.dumps(captured_sample_cz())))
    body = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert body["tuNo"] == AWB
    url = session.get.call_args[0][0]
    assert HOST in url
    assert "rstt028" in url
    assert AWB in url
    assert POSTAL_CODE in url


async def test_transport_strips_whitespace_from_postal_code():
    import json as _json

    session = _session_with(_get_ctx(200, _json.dumps(captured_sample_cz())))
    await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "254 01")
    url = session.get.call_args[0][0]
    assert "postalCode=25401" in url


async def test_transport_e800_returns_none():
    session = _session_with(_get_ctx(404, '{"lastError":"E800","exceptionText":"x"}'))
    result = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert result is None


async def test_transport_e206_returns_none():
    session = _session_with(_get_ctx(404, '{"lastError":"E206","exceptionText":"x"}'))
    result = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert result is None


async def test_transport_e609_falls_back_to_rstt029(caplog):
    import json as _json

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
        body = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert body["tuNo"] == AWB
    assert "history" not in body  # the fallback never carries it
    second_url = session.get.call_args_list[1][0][0]
    assert "rstt029" in second_url
    assert AWB in second_url
    assert any("postal code" in m.lower() for m in caplog.messages)


async def test_transport_e609_postcode_warning_fires_once_per_parcel(caplog):
    import json as _json

    fallback_body = {"tuStatus": [{"tuNo": AWB, "progressBar": {}}]}
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, _json.dumps(fallback_body)),
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, _json.dumps(fallback_body)),
    )
    with caplog.at_level("WARNING"):
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")
    mismatch_warnings = [m for m in caplog.messages if "does not match" in m]
    assert len(mismatch_warnings) == 1


async def test_transport_e609_fallback_with_no_tu_status_returns_none():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, '{"tuStatus": []}'),
    )
    result = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert result is None


async def test_transport_fallback_semantic_error_returns_none():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(404, '{"lastError":"E000"}'),
    )
    result = await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")
    assert result is None


async def test_transport_maintenance_page_raises_and_warns_once(caplog):
    session = _session_with(
        _get_ctx(200, "<html>Maintenance</html>"),
        _get_ctx(200, "<html>Maintenance</html>"),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(GlsGroupMaintenanceError):
            await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
        with pytest.raises(GlsGroupMaintenanceError):
            await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
    assert issubclass(GlsGroupMaintenanceError, GlsApiError)
    outage_warnings = [m for m in caplog.messages if "down" in m.lower()]
    assert len(outage_warnings) == 1


async def test_transport_maintenance_page_on_fallback_also_raises():
    session = _session_with(
        _get_ctx(404, '{"lastError":"E609"}'),
        _get_ctx(200, "<html>Maintenance</html>"),
    )
    with pytest.raises(GlsGroupMaintenanceError):
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, "00000")


async def test_transport_raises_gls_api_error_on_unexpected_status():
    session = _session_with(_get_ctx(500, ""))
    with pytest.raises(GlsApiError):
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)


async def test_transport_raises_on_malformed_postcode_field_exception():
    """A 400 with a valid JSON body but no ``lastError`` (a malformed-postcode
    field exception, group-rest.md) is still a hard error, not a semantic miss."""
    body = (
        '{"fieldExceptions":[{"attribute":"zipcode","exceptionText":"x"}],'
        '"exceptionText":"x"}'
    )
    session = _session_with(_get_ctx(400, body))
    with pytest.raises(GlsApiError):
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)


async def test_transport_empty_body_is_treated_as_outage():
    session = _session_with(_get_ctx(200, ""))
    with pytest.raises(GlsGroupMaintenanceError):
        await async_get_parcel_cz(session, HOST, GROUP_LOCALE, AWB, POSTAL_CODE)
