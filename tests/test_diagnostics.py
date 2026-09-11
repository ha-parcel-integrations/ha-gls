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


async def test_diagnostics_redacts_detailed_canadian_raw_payload(hass):
    entry = MagicMock()
    entry.data = {}
    entry.options = {"country": "CA", "parcels": [{"parcel_no": "CA12345678"}]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "CA12345678",
            "raw_status": "Delivered",
            "raw": {
                "shipments": [
                    {
                        "trackingNumber": "CA12345678",
                        "consignee": {"city": "Example City"},
                        "billingAccount": "account-123",
                        "currentStatus": {"name": "Delivered"},
                        "references": [{"value": "customer-order"}],
                        "parcels": [
                            {
                                "parcelId": 1,
                                "activities": [
                                    {
                                        "status": "Delivered",
                                        "activityDate": "2026-08-28 14:52:32",
                                        "statusDetail": "Front door",
                                        "terminal": "Example terminal",
                                        "latitude": 45.42,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(hass, entry)
    shipment = result["incoming"][0]["raw"]["shipments"][0]
    assert shipment["trackingNumber"] == "**REDACTED**"
    assert shipment["consignee"] == "**REDACTED**"
    assert shipment["billingAccount"] == "**REDACTED**"
    assert shipment["references"] == "**REDACTED**"
    assert shipment["parcels"][0]["parcelId"] == "**REDACTED**"
    activity = shipment["parcels"][0]["activities"][0]
    assert activity["statusDetail"] == "**REDACTED**"
    assert activity["latitude"] == "**REDACTED**"
    # The status vocabulary must survive: it is what an unrecognised-status
    # report needs from the attached diagnostics.
    assert result["incoming"][0]["raw_status"] == "Delivered"
    assert activity["status"] == "Delivered"
    assert activity["activityDate"] == "2026-08-28 14:52:32"
    assert activity["terminal"] == "Example terminal"


async def test_diagnostics_redacts_the_us_delivery_and_pod_fields(hass):
    entry = MagicMock()
    entry.data = {}
    entry.options = {"country": "US", "parcels": [{"parcel_no": "12345678901234"}]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "12345678901234",
            "raw_status": "SHIPMENT DELIVERED",
            "raw": {
                "shipments": [
                    {
                        "trackingNumber": "12345678901234",
                        "controlNumber": "0000001",
                        "status": "SHIPMENT DELIVERED",
                        "deliveryAddress": "1 Example Street",
                        "signedBy": "Synthetic signer",
                        "deliveredAt": "Front door",
                        "podSignatureId": "sig-1",
                        "stopId": "stop-1",
                        "transitDetails": [
                            {
                                "eventDateTime": "2026-09-09T11:04:00",
                                "eventDetails": "SHIPMENT DELIVERED",
                                "location": "Example Hub",
                            }
                        ],
                    }
                ]
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None

    result = await async_get_config_entry_diagnostics(hass, entry)
    shipment = result["incoming"][0]["raw"]["shipments"][0]
    assert shipment["trackingNumber"] == "**REDACTED**"
    assert shipment["controlNumber"] == "**REDACTED**"
    assert shipment["deliveryAddress"] == "**REDACTED**"
    assert shipment["signedBy"] == "**REDACTED**"
    assert shipment["deliveredAt"] == "**REDACTED**"
    assert shipment["podSignatureId"] == "**REDACTED**"
    assert shipment["stopId"] == "**REDACTED**"
    # The status vocabulary must survive: it is what an unrecognised-status
    # report needs from the attached diagnostics.
    assert result["incoming"][0]["raw_status"] == "SHIPMENT DELIVERED"
    assert shipment["status"] == "SHIPMENT DELIVERED"
    detail = shipment["transitDetails"][0]
    assert detail["eventDetails"] == "SHIPMENT DELIVERED"
    assert detail["eventDateTime"] == "2026-09-09T11:04:00"


async def test_diagnostics_redacts_cz_custref_and_signature_value(hass):
    """CUSTREF (the sender's own order reference), UNITNO (the parcel number
    under another name, ha-gls#6) and signature.value are the genuinely
    sensitive CZ fields — a WEIGHT reference's own value must survive
    untouched, proving the redaction is type-scoped, not a blanket "value"
    key match. referenceNo/tuNo carry the same parcel number as UNITNO
    (ha-gls#6) and are redacted by the flat TO_REDACT key set."""
    entry = MagicMock()
    entry.data = {}
    entry.options = {"country": "CZ", "parcels": []}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "1234567890",
            "raw": {
                "postalCode": "11000",
                "referenceNo": "1234567890",
                "tuNo": "1234567890",
                "signature": {"validate": True, "name": "Signature:", "value": "true"},
                "references": [
                    {"type": "UNITNO", "name": "Parcel number:", "value": "1234567890"},
                    {"type": "CUSTREF", "name": "Reference no:", "value": "order-42"},
                    {"type": "WEIGHT", "name": "Weight:", "value": "0.9 kg"},
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
    assert incoming_raw["referenceNo"] == "**REDACTED**"
    assert incoming_raw["tuNo"] == "**REDACTED**"
    assert incoming_raw["signature"]["value"] == "**REDACTED**"
    references = {r["type"]: r["value"] for r in incoming_raw["references"]}
    assert references["CUSTREF"] == "**REDACTED**"
    assert references["UNITNO"] == "**REDACTED**"
    assert references["WEIGHT"] == "0.9 kg"  # never touched
