"""Tests for GLS diagnostics."""
from unittest.mock import MagicMock

from custom_components.gls.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    entry = MagicMock()
    entry.options = {"parcels": [{"parcel_no": "123", "postal_code": "1234AB"}]}
    entry.runtime_data.coordinator.data = [
        {"barcode": "123", "sender": "S", "raw": {"zipcode": "1234AB", "city": "Amsterdam"}}
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # postal_code / parcel_no in the options are redacted
    assert result["entry_options"]["parcels"][0]["parcel_no"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["city"] == "**REDACTED**"
