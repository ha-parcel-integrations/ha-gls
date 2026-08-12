"""Tests for the GLS config and options flow."""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DOMAIN,
)
from custom_components.gls.countries.de.session import GlsDeSessionError


async def test_user_flow_creates_hub_with_postcode_only(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234 ab"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS (1234AB)"
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
    assert result["description_placeholders"]["postcode_example"] == "1234AB"


async def test_user_flow_invalid_postcode_keeps_selected_country(hass):
    """Regression test for ha-parcel-integrations/ha-gls#4.

    An invalid postcode used to re-show the form defaulted back to NL,
    even when the user had picked DE — and the error/help text always
    referred to the Dutch postcode format regardless of country.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "de", CONF_POSTAL_CODE: "nope"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"
    assert result["description_placeholders"]["postcode_example"] == "12345"
    country_key = next(k for k in result["data_schema"].schema if k == CONF_COUNTRY)
    # The selector itself only accepts lower-case option values (hassfest
    # translation-key rule); the stored/internal value stays upper-case.
    assert country_key.default() == "de"


async def test_same_postcode_hub_rejected(hass):
    """A second hub for the same postcode aborts; the postcode is the key."""
    MockConfigEntry(domain=DOMAIN, unique_id="1234AB").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_second_hub_different_postcode_allowed(hass):
    """A hub for a different postcode is allowed (home + work)."""
    MockConfigEntry(domain=DOMAIN, unique_id="1234AB").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "5678CD"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS (5678CD)"


# ---------------------------------------------------------------------------
# DE registration step
# ---------------------------------------------------------------------------


async def test_de_user_flow_registers_and_stores_app_instance_id(hass):
    with patch(
        "custom_components.gls.config_flow.GlsDeSession.async_register",
        new=AsyncMock(return_value="11111111-2222-3333-4444-555555555555"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_COUNTRY: "de", CONF_POSTAL_CODE: "12345"},
        )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "DE"
    assert result["data"][CONF_DE_APP_INSTANCE_ID] == (
        "11111111-2222-3333-4444-555555555555"
    )


async def test_de_user_flow_registration_failure_shows_error(hass):
    with patch(
        "custom_components.gls.config_flow.GlsDeSession.async_register",
        new=AsyncMock(side_effect=GlsDeSessionError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_COUNTRY: "de", CONF_POSTAL_CODE: "12345"},
        )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "de_registration_failed"


async def test_nl_user_flow_does_not_touch_de_session(hass):
    """NL setup must never call the DE registration path at all."""
    with patch(
        "custom_components.gls.config_flow.GlsDeSession.async_register",
        new=AsyncMock(side_effect=AssertionError("should not be called for NL")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_COUNTRY: "nl", CONF_POSTAL_CODE: "1234AB"}
        )
    assert result["type"] == "create_entry"
    assert result["data"] == {}


def _hub(parcels: list[dict], *, country: str | None = None) -> MockConfigEntry:
    options = {CONF_PARCELS: parcels, CONF_POSTAL_CODE: "1000AA"}
    if country is not None:
        options[CONF_COUNTRY] = country
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="1000AA",
        options=options,
    )


def _init_input(
    *, add="", remove=None, interval="30", history=False,
    filter_type="days", amount=7,
) -> dict:
    """Build the sectioned options-form submission."""
    parcels: dict = {"add": add}
    if remove is not None:
        parcels["remove"] = remove
    return {
        "parcels": parcels,
        "delivered": {
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
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


async def test_options_flow_preserves_de_country(hass):
    """Adding a parcel through the options flow must not reset the hub's country.

    Regression test for ha-parcel-integrations/ha-gls#2: an options flow's
    ``data`` replaces ``entry.options`` wholesale rather than merging into
    it, so omitting CONF_COUNTRY here silently downgraded every DE hub to
    NL the moment its first (mandatory, since CONF_PARCELS starts empty)
    parcel was added.
    """
    entry = _hub([], country="DE")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="222222222")
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_COUNTRY] == "DE"


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


async def test_options_changes_interval_history_and_delivered(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _init_input(interval="120", history=True, filter_type="parcels", amount=5),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 120
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
