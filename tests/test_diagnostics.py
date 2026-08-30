"""Tests for GLS diagnostics."""
import json
from unittest.mock import MagicMock

from custom_components.gls.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    entry = MagicMock()
    entry.data = {}
    entry.options = {"parcels": [{"parcel_no": "123", "postal_code": "1234AB"}]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "123",
            "sender": "S",
            "receiver": "R",
            "pickup_point": "GLS ParcelShop Voorbeeldstraat",
            "url": "https://www.gls-info.nl/tracking?trackid=123&zipcode=1234AB",
            "raw": {"zipcode": "1234AB", "city": "Amsterdam"},
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # postal_code / parcel_no in the options are redacted
    assert result["entry_options"]["parcels"][0]["parcel_no"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["city"] == "**REDACTED**"
    # canonical top-level fields — a parcel's own "url" embeds the tracking
    # number and postal code as query params, which redaction can't scrub
    # partially, so the whole field must be blanked.
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["sender"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["pickup_point"] == "**REDACTED**"
    assert result["incoming"][0]["url"] == "**REDACTED**"


async def test_diagnostics_redacts_de_app_instance_id_and_tokens(hass):
    """No appInstanceId and no token string may appear anywhere in the output."""
    app_instance_id = "11111111-2222-3333-4444-555555555555"
    fake_token = "eyJhbGciOiJSUzI1NiJ9.super-secret-jwt-body.signature"

    entry = MagicMock()
    entry.data = {"de_app_instance_id": app_instance_id}
    entry.options = {
        "country": "DE",
        "parcels": [
            {
                "parcel_no": "075624238061",
                "postal_code": "00000",
                "de_parcel_number": "YOXVB8CE",
            }
        ],
    }
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "075624238061",
            "sender": None,
            "raw": {
                "id": "11111111-aaaa-bbbb-cccc-222222222222",
                "trackingReference": "075624238061",
                "parcelNumber": "YOXVB8CE",
                # defensive: even if a token or the id ever rode along in a
                # raw payload, it must still be redacted.
                "appInstanceId": app_instance_id,
                "accessToken": fake_token,
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(hass, entry)

    dumped = json.dumps(result)
    assert app_instance_id not in dumped
    assert fake_token not in dumped
    assert result["entry_data"]["de_app_instance_id"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["appInstanceId"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["accessToken"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["parcelNumber"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["id"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["trackingReference"] == "**REDACTED**"
    assert result["entry_options"]["parcels"][0]["de_parcel_number"] == "**REDACTED**"


async def test_diagnostics_redacts_cz_custref_and_signature_value(hass):
    """CUSTREF (the sender's own order reference) and signature.value are the
    two genuinely sensitive CZ fields (BUILD_PLAN_CZ.md §7) — UNITNO's own
    value must survive untouched, proving the redaction is type-scoped, not a
    blanket "value" key match."""
    entry = MagicMock()
    entry.data = {}
    entry.options = {"country": "CZ", "parcels": []}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "5036234901",
            "raw": {
                "postalCode": "25401",
                "signature": {"validate": True, "name": "Signature:", "value": "true"},
                "references": [
                    {"type": "UNITNO", "name": "Parcel number:", "value": "5036234901"},
                    {"type": "CUSTREF", "name": "Reference no:", "value": "order-42"},
                ],
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(hass, entry)

    incoming_raw = result["incoming"][0]["raw"]
    assert incoming_raw["postalCode"] == "**REDACTED**"
    assert incoming_raw["signature"]["value"] == "**REDACTED**"
    references = {r["type"]: r["value"] for r in incoming_raw["references"]}
    assert references["CUSTREF"] == "**REDACTED**"
    assert references["UNITNO"] == "5036234901"  # never touched
