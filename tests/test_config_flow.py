"""Tests for the GLS config and options flow."""
from unittest.mock import AsyncMock, patch

import pytest
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
    COUNTRIES,
    DEFAULT_NEW_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_AUTO,
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
    # New hubs default to dynamic polling (dynamic-polling.md Section 5.2).
    assert result["options"][CONF_REFRESH_INTERVAL] == DEFAULT_NEW_REFRESH_INTERVAL
    assert DEFAULT_NEW_REFRESH_INTERVAL == REFRESH_INTERVAL_AUTO


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


async def test_cz_user_flow_creates_hub_with_spaced_postcode(hass):
    """CZ postcodes are written with or without the internal space; either
    form must be accepted and stored space-stripped (the
    unspaced form is the verified-working one sent on the wire)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "cz", CONF_POSTAL_CODE: "110 00"}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "CZ"
    assert result["options"][CONF_POSTAL_CODE] == "11000"


async def test_cz_user_flow_invalid_postcode(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "cz", CONF_POSTAL_CODE: "nope"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"
    assert result["description_placeholders"]["postcode_example"] == "110 00"


async def test_same_postcode_hub_rejected(hass):
    """A second hub for the same postcode+country aborts (unique_id is now
    f"{country}:{postal_code}")."""
    MockConfigEntry(domain=DOMAIN, unique_id="NL:1234AB").add_to_hass(hass)
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
    MockConfigEntry(domain=DOMAIN, unique_id="NL:1234AB").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "5678CD"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS (5678CD)"


async def test_same_postcode_different_country_hub_allowed(hass):
    """The same postcode string in a different country is a different hub —
    French and German postcodes can both read "39100"."""
    MockConfigEntry(domain=DOMAIN, unique_id="DE:39100").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "fr", CONF_POSTAL_CODE: "39100"}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "FR"


# ---------------------------------------------------------------------------
# The Group-leaf countries added past their research gate
# — postcode validation only; the transport
# itself is covered in tests/countries/test_group.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country", ["SK", "AT", "IE", "FR", "SI", "HR", "IT"])
async def test_group_leaf_country_user_flow_accepts_its_own_postcode_example(
    hass, country
):
    example = COUNTRIES[country]["postcode_example"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: country.lower(), CONF_POSTAL_CODE: example},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == country
    assert result["options"][CONF_POSTAL_CODE] == example.replace(" ", "")


@pytest.mark.parametrize("country", ["SK", "AT", "IE", "FR", "SI", "HR", "IT"])
async def test_group_leaf_country_user_flow_rejects_bad_postcode(hass, country):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: country.lower(), CONF_POSTAL_CODE: "!!!"},
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"
    assert (
        result["description_placeholders"]["postcode_example"]
        == COUNTRIES[country]["postcode_example"]
    )


async def test_ie_eircode_accepts_spaced_form(hass):
    """Eircodes are written with an internal space; the regex normalises it
    away first (space-stripped, upper-cased) — the shakiest entry in COUNTRIES."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "ie", CONF_POSTAL_CODE: "d02 af30"},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_POSTAL_CODE] == "D02AF30"


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


async def _open_options_step(hass, entry, step_id: str):
    """Start the options flow and select one of its two top-level routes."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_parcel_list_can_be_cleared(hass):
    """A submitted empty list removes the final manually tracked parcel."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_PARCEL_NO: "EXAMPLE111111"}], CONF_POSTAL_CODE: "1234AB"})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_options_settings_preserve_parcel_list(hass):
    """Saving settings must never replace the manually tracked parcel list."""
    parcels = [{CONF_PARCEL_NO: "EXAMPLE111111"}]
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: parcels, CONF_POSTAL_CODE: "1234AB"})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7, CONF_INCLUDE_HISTORY: False, CONF_REFRESH_INTERVAL: "30"}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == parcels


async def test_options_settings_can_switch_to_auto(hass):
    """An existing fixed-interval hub can opt into dynamic polling."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: "1234AB", CONF_REFRESH_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_REFRESH_INTERVAL: REFRESH_INTERVAL_AUTO,
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == REFRESH_INTERVAL_AUTO
