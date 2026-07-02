"""Tests for the GLS config and options flow."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
)


async def test_user_flow_creates_hub_with_postcode_only(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234 ab"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS"
    assert result["options"][CONF_PARCELS] == []
    assert result["options"][CONF_POSTAL_CODE] == "1234AB"


async def test_user_flow_invalid_postcode(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "nope"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


async def test_only_one_instance_allowed(hass):
    """single_config_entry aborts a second flow before the form is shown."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels, CONF_POSTAL_CODE: "1000AA"},
    )


def _init_input(*, add="", remove=None, interval="30", history=False) -> dict:
    """Build the sectioned options-form submission."""
    parcels: dict = {"add": add}
    if remove is not None:
        parcels["remove"] = remove
    return {
        "parcels": parcels,
        "history": {CONF_INCLUDE_HISTORY: history},
        "polling": {CONF_REFRESH_INTERVAL: interval},
    }


async def test_options_add_parcel_uses_hub_postcode(hass):
    entry = _hub([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # No postcode field at all — the hub default is used.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="222222222")
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_PARCEL_NO: "222222222", CONF_POSTAL_CODE: "1000AA"}
    ]


async def test_options_add_alphanumeric_tracking_id(hass):
    """The short alphanumeric uniqueNo is accepted and upper-cased."""
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="00l1b3bx")
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_PARCEL_NO: "00L1B3BX", CONF_POSTAL_CODE: "1000AA"}
    ]


async def test_options_add_invalid_parcel_no(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="abc")
    )
    assert result["errors"]["base"] == "invalid_parcel_no"


async def test_options_add_duplicate_rejected(hass):
    entry = _hub([{CONF_PARCEL_NO: "111111111", CONF_POSTAL_CODE: "1000AA"}])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="111111111", remove=[])
    )
    assert result["errors"]["base"] == "already_tracked"


async def test_options_remove_parcel(hass):
    entry = _hub([
        {CONF_PARCEL_NO: "111111111", CONF_POSTAL_CODE: "1000AA"},
        {CONF_PARCEL_NO: "222222222", CONF_POSTAL_CODE: "2000BB"},
    ])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(remove=["111111111"])
    )
    assert result["type"] == "create_entry"
    nos = {p[CONF_PARCEL_NO] for p in result["data"][CONF_PARCELS]}
    assert nos == {"222222222"}


async def test_options_changes_interval_and_history(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(interval="120", history=True)
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 120
    assert result["data"][CONF_INCLUDE_HISTORY] is True
