"""Tests for the GLS config and options flow."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.account.countries.dk.session import (
    GlsDkApiError,
    GlsDkAuthError,
)
from custom_components.gls.const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    COUNTRIES,
    DOMAIN,
)
from custom_components.gls.tracking.countries.de.session import GlsDeSessionError

from .payloads import minimal_sample


async def _open_tracking_flow(hass):
    """Open the setup flow and pick tracking numbers from its menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )


async def test_setup_opens_on_a_menu_choosing_account_or_tracking(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["account", "tracking"]


async def test_user_flow_creates_hub_with_postcode_only(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234 ab"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS (1234AB)"
    assert result["options"][CONF_PARCELS] == []
    assert result["options"][CONF_POSTAL_CODE] == "1234AB"
    # Polling cadence is not a stored option — it is always status-driven.
    assert "refresh_interval" not in result["options"]


async def test_user_flow_invalid_postcode(hass):
    result = await _open_tracking_flow(hass)
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
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "de", CONF_POSTAL_CODE: "nope"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"
    assert result["description_placeholders"]["postcode_example"] == "12345"
    country_key = next(k for k in result["data_schema"].schema if k == CONF_COUNTRY)
    # The selector itself only accepts lower-case option values (hassfest
    # translation-key rule); the stored/internal value stays upper-case.
    assert country_key.default() == "de"


async def test_country_dropdown_is_sorted_by_its_translated_label(hass):
    """The frontend alphabetises the dropdown; sorting the ISO codes here
    would put Germany above Denmark, and be wrong in every language but
    English anyway."""
    result = await _open_tracking_flow(hass)
    country_key = next(k for k in result["data_schema"].schema if k == CONF_COUNTRY)
    config = result["data_schema"].schema[country_key].config
    assert config["sort"] is True
    assert set(config["options"]) == {code.lower() for code in COUNTRIES}


async def test_cz_user_flow_creates_hub_with_spaced_postcode(hass):
    """CZ postcodes are written with or without the internal space; either
    form must be accepted and stored space-stripped (the
    unspaced form is the verified-working one sent on the wire)."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "cz", CONF_POSTAL_CODE: "110 00"}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "CZ"
    assert result["options"][CONF_POSTAL_CODE] == "11000"


async def test_cz_user_flow_invalid_postcode(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "cz", CONF_POSTAL_CODE: "nope"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"
    assert result["description_placeholders"]["postcode_example"] == "110 00"


async def test_ca_user_flow_normalizes_canadian_postcode(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "ca", CONF_POSTAL_CODE: "k1a 0b1"}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "CA"
    assert result["options"][CONF_POSTAL_CODE] == "K1A0B1"


async def test_ca_user_flow_rejects_non_canadian_postcode(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "ca", CONF_POSTAL_CODE: "12345"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


@pytest.mark.parametrize(
    ("entered", "stored"),
    [("90210", "90210"), ("90210-1234", "90210-1234"), ("902101234", "902101234")],
)
async def test_us_user_flow_accepts_zip_and_zip_plus_four(hass, entered, stored):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "us", CONF_POSTAL_CODE: entered}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "US"
    assert result["options"][CONF_POSTAL_CODE] == stored


async def test_us_user_flow_rejects_a_non_us_postcode(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "us", CONF_POSTAL_CODE: "K1A 0B1"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


@pytest.mark.parametrize("entered", ["00-001", "00001"])
async def test_pl_user_flow_accepts_both_postcode_spellings(hass, entered):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "pl", CONF_POSTAL_CODE: entered}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "PL"
    assert result["options"][CONF_POSTAL_CODE] == entered


async def test_pl_user_flow_rejects_a_non_polish_postcode(hass):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "pl", CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


async def test_same_postcode_hub_rejected(hass):
    """A second hub for the same postcode+country aborts (unique_id is now
    f"{country}:{postal_code}")."""
    MockConfigEntry(domain=DOMAIN, unique_id="NL:1234AB").add_to_hass(hass)
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_second_hub_different_postcode_allowed(hass):
    """A hub for a different postcode is allowed (home + work)."""
    MockConfigEntry(domain=DOMAIN, unique_id="NL:1234AB").add_to_hass(hass)
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "5678CD"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS (5678CD)"


async def test_same_postcode_different_country_hub_allowed(hass):
    """The same postcode string in a different country is a different hub —
    French and German postcodes can both read "39100"."""
    MockConfigEntry(domain=DOMAIN, unique_id="DE:39100").add_to_hass(hass)
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "fr", CONF_POSTAL_CODE: "39100"}
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == "FR"


# ---------------------------------------------------------------------------
# The Group-leaf countries without a real capture
# — postcode validation only; the transport
# itself is covered in tests/countries/test_group.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "country",
    [
        "BE",
        "CH",
        "DK",
        "FI",
        "HU",
        "LI",
        "LU",
        "RS",
        "SE",
        "SK",
        "AT",
        "IE",
        "FR",
        "SI",
        "HR",
        "IT",
    ],
)
async def test_group_leaf_country_user_flow_accepts_its_own_postcode_example(
    hass, country
):
    example = COUNTRIES[country]["postcode_example"]
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: country.lower(), CONF_POSTAL_CODE: example},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == country
    assert result["options"][CONF_POSTAL_CODE] == example.replace(" ", "")


@pytest.mark.parametrize(
    "country",
    [
        "BE",
        "CH",
        "DK",
        "FI",
        "HU",
        "LI",
        "LU",
        "RS",
        "SE",
        "SK",
        "AT",
        "IE",
        "FR",
        "SI",
        "HR",
        "IT",
    ],
)
async def test_group_leaf_country_user_flow_rejects_bad_postcode(hass, country):
    result = await _open_tracking_flow(hass)
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
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "ie", CONF_POSTAL_CODE: "d02 af30"},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_POSTAL_CODE] == "D02AF30"


@pytest.mark.parametrize("postcode", ["111 22", "11122"])
async def test_se_accepts_both_written_forms_of_a_postcode(hass, postcode):
    """Swedish postcodes are written "NNN NN" and the space is normalised away
    before matching, so the hub stores the same value either way."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "se", CONF_POSTAL_CODE: postcode},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_POSTAL_CODE] == "11122"


@pytest.mark.parametrize("postcode", ["9485", "9490", "9498"])
async def test_li_accepts_every_postcode_in_the_reserved_block(hass, postcode):
    """Liechtenstein's whole postcode range is 9485-9498, so the rule is a
    block rather than CH's generic four digits — the ends must both pass."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "li", CONF_POSTAL_CODE: postcode},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_POSTAL_CODE] == postcode


@pytest.mark.parametrize("postcode", ["9484", "9499", "8001"])
async def test_li_rejects_a_swiss_postcode_outside_the_block(hass, postcode):
    """A Swiss postcode is valid for CH and wrong for an LI hub; 8001 is CH's
    own example, and 9484/9499 bracket the block."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "li", CONF_POSTAL_CODE: postcode},
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


# ---------------------------------------------------------------------------
# DE registration step
# ---------------------------------------------------------------------------


async def test_de_user_flow_registers_and_stores_app_instance_id(hass):
    with patch(
        "custom_components.gls.config_flow.GlsDeSession.async_register",
        new=AsyncMock(return_value="11111111-2222-3333-4444-555555555555"),
    ):
        result = await _open_tracking_flow(hass)
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
        result = await _open_tracking_flow(hass)
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
        result = await _open_tracking_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_COUNTRY: "nl", CONF_POSTAL_CODE: "1234AB"}
        )
    assert result["type"] == "create_entry"
    assert result["data"] == {"source": "tracking"}


def _hub(parcels: list[dict], *, country: str | None = None) -> MockConfigEntry:
    options = {CONF_PARCELS: parcels, CONF_POSTAL_CODE: "1000AA"}
    if country is not None:
        options[CONF_COUNTRY] = country
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="1000AA",
        options=options,
    )


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
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_PARCELS: [{CONF_PARCEL_NO: "EXAMPLE111111"}],
            CONF_POSTAL_CODE: "1234AB",
        },
    )
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
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_PARCELS: parcels, CONF_POSTAL_CODE: "1234AB"}
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == parcels


async def test_options_settings_has_no_polling_field(hass):
    """The polling cadence is no longer user-configurable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_PARCELS: [], CONF_POSTAL_CODE: "1234AB"},
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    assert "refresh_interval" not in result["data_schema"].schema


@pytest.mark.parametrize(("country", "postcode"), [("es", "28001"), ("pt", "1000-001")])
async def test_es_and_pt_user_flow_accept_their_own_postcode_example(
    hass, country, postcode
):
    """For ES/PT the postcode is half the lookup key, not hub validation — so
    it has to be accepted in the country's own written form."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: country, CONF_POSTAL_CODE: postcode},
    )
    assert result["type"] == "create_entry"
    assert result["options"][CONF_COUNTRY] == country.upper()


async def test_pt_postcode_accepts_the_unhyphenated_form(hass):
    """Normalisation only strips spaces, so the hyphen is optional the way
    PL's is."""
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: "pt", CONF_POSTAL_CODE: "1000001"},
    )
    assert result["type"] == "create_entry"


@pytest.mark.parametrize("country", ["es", "pt"])
async def test_es_and_pt_reject_a_bad_postcode(hass, country):
    result = await _open_tracking_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COUNTRY: country, CONF_POSTAL_CODE: "!!!"},
    )
    assert result["errors"][CONF_POSTAL_CODE] == "invalid_postcode"


# ---------------------------------------------------------------------------
# Country dropdown labels
# ---------------------------------------------------------------------------


_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "gls"


def _country_label_block(payload: dict) -> dict | None:
    """Find the selector options block that holds the country labels."""
    if isinstance(payload, dict):
        options = payload.get("options")
        if isinstance(options, dict) and {"nl", "de"} <= set(options):
            return options
        for value in payload.values():
            found = _country_label_block(value)
            if found is not None:
                return found
    return None


@pytest.mark.parametrize(
    "path",
    [
        _COMPONENT / "strings.json",
        *sorted((_COMPONENT / "translations").glob("*.json")),
    ],
    ids=lambda path: path.name,
)
def test_every_country_has_a_dropdown_label(path):
    """A country in ``COUNTRIES`` with no label shows the raw option key
    ("es" instead of "Spain") in the setup dropdown, because the selector's
    option values *are* the translation keys. Adding a country touches every
    translation file, so this asserts the set rather than trusting the edit."""
    labels = _country_label_block(json.loads(path.read_text(encoding="utf-8")))
    assert labels is not None, f"{path.name} has no country options block"
    assert set(labels) == {code.lower() for code in COUNTRIES}


_ACCOUNT_STATE = {
    "uid": "example-user",
    "email": "parcel@example.test",
    "refresh_token": "private-refresh",
}


async def _open_account_flow(hass):
    """Open the setup flow on the Danish sign-in form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "dk"}
    )


async def test_account_route_asks_for_the_country_before_signing_in(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "account"
    country_key = next(k for k in result["data_schema"].schema if k == CONF_COUNTRY)
    assert result["data_schema"].schema[country_key].config["options"] == ["dk", "pl"]
    assert country_key.default() == "dk"
    assert CONF_POSTAL_CODE not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "dk"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "dk"


async def test_danish_account_route_needs_no_postcode_and_discards_password(hass):
    result = await _open_account_flow(hass)
    assert result["step_id"] == "dk"
    with (
        patch(
            "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
            new=AsyncMock(return_value=_ACCOUNT_STATE),
        ),
        patch("custom_components.gls.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "parcel@example.test", "password": "private-password"},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS Denmark account"
    assert result["data"] == {"source": "account", **_ACCOUNT_STATE}
    assert result["result"].unique_id == "account:example-user"
    assert result["options"][CONF_COUNTRY] == "DK"
    assert CONF_POSTAL_CODE not in result["options"]
    assert "password" not in result["data"]


@pytest.mark.parametrize(
    ("failure", "error"), [("auth", "invalid_auth"), ("transport", "cannot_connect")]
)
async def test_danish_account_login_errors_are_sanitized(hass, failure, error):
    result = await _open_account_flow(hass)
    exc = GlsDkAuthError("private") if failure == "auth" else GlsDkApiError("private")
    with patch(
        "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
        new=AsyncMock(side_effect=exc),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "parcel@example.test", "password": "private-password"},
        )
    assert result["errors"] == {"base": error}


async def test_danish_account_duplicate_is_rejected(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-user",
        data={"source": "account", **_ACCOUNT_STATE},
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    result = await _open_account_flow(hass)
    with patch(
        "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
        new=AsyncMock(return_value=_ACCOUNT_STATE),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "parcel@example.test", "password": "private-password"},
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_danish_account_reauth_updates_same_account_only(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-user",
        data={"source": "account", **_ACCOUNT_STATE},
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    assert list(result["data_schema"].schema) == ["password"]
    assert result["description_placeholders"]["identifier"] == "parcel@example.test"
    renewed = {**_ACCOUNT_STATE, "refresh_token": "rotated-refresh"}
    with (
        patch(
            "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
            new=AsyncMock(return_value=renewed),
        ) as login,
        patch.object(
            hass.config_entries, "async_reload", return_value=True
        ) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-private-password"}
        )
        await hass.async_block_till_done()
    login.assert_awaited_once_with("parcel@example.test", "new-private-password")
    assert result["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "rotated-refresh"
    assert "password" not in entry.data
    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_danish_account_reauth_cannot_switch_user(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-user",
        data={"source": "account", **_ACCOUNT_STATE},
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    with patch(
        "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
        new=AsyncMock(return_value={**_ACCOUNT_STATE, "uid": "other-user"}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "private-password"}
        )
    assert result["reason"] == "reauth_account_mismatch"
    assert entry.data["uid"] == "example-user"


@pytest.mark.parametrize(
    ("failure", "error"), [("auth", "invalid_auth"), ("transport", "cannot_connect")]
)
async def test_danish_account_reauth_errors_keep_the_password_form(
    hass, failure, error
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-user",
        data={"source": "account", **_ACCOUNT_STATE},
        options={CONF_COUNTRY: "DK"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    exc = GlsDkAuthError("private") if failure == "auth" else GlsDkApiError("private")
    with patch(
        "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
        new=AsyncMock(side_effect=exc),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "private-password"}
        )
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert entry.data["refresh_token"] == "private-refresh"


async def test_manual_hub_does_not_offer_account_reauth(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="NL:1234AB", data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["reason"] == "reauth_not_supported"


_ACCOUNT_FEED = {"totalCount": 0, "items": []}
_SESSION_FEED = (
    "custom_components.gls.account.countries.dk.session.GlsDkSession.async_get_parcels"
)


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:example-user",
        data={"source": "account", **_ACCOUNT_STATE},
        options={CONF_COUNTRY: "DK"},
    )


async def test_danish_account_reauth_of_a_running_entry_reloads_it(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    feed = AsyncMock(return_value=_ACCOUNT_FEED)
    renewed = {**_ACCOUNT_STATE, "refresh_token": "rotated-refresh"}
    with (
        patch(_SESSION_FEED, new=feed),
        patch(
            "custom_components.gls.account.countries.dk.session.GlsDkSession.async_login",
            new=AsyncMock(return_value=renewed),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-private-password"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["refresh_token"] == "rotated-refresh"
    assert feed.await_count == 2


async def test_account_options_offer_settings_and_refresh_in_place(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    feed = AsyncMock(return_value=_ACCOUNT_FEED)
    with (
        patch(_SESSION_FEED, new=feed),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert len(entry.update_listeners) == 1
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == "form"
        assert result["step_id"] == "settings"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DELIVERED_FILTER_TYPE: "parcels",
                CONF_DELIVERED_FILTER_AMOUNT: 3,
                CONF_INCLUDE_HISTORY: True,
            },
        )
        await hass.async_block_till_done()
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert CONF_PARCELS not in result["data"]
    assert entry.options[CONF_INCLUDE_HISTORY] is True
    assert entry.state is ConfigEntryState.LOADED
    reload_entry.assert_not_called()
    assert feed.await_count == 2


async def test_manual_hub_options_refresh_in_place_without_a_reload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:1234AB",
        options={CONF_COUNTRY: "NL", CONF_POSTAL_CODE: "1234AB", CONF_PARCELS: []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gls.tracking.api.GlsApiClient.async_get_parcel",
        new=AsyncMock(return_value=minimal_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert len(entry.update_listeners) == 1
        with (
            patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
            patch.object(
                entry.runtime_data.coordinator, "async_request_refresh", new=AsyncMock()
            ) as refresh,
        ):
            result = await _open_options_step(hass, entry, "parcels")
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"tracking_codes": ["EXAMPLE111111"]}
            )
            await hass.async_block_till_done()
        assert result["type"] == "create_entry"
        reload_entry.assert_not_called()
        refresh.assert_awaited_once()
        assert entry.options[CONF_PARCELS] == [{CONF_PARCEL_NO: "EXAMPLE111111"}]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_parcels_step_keeps_the_stored_item_of_a_kept_code(hass):
    kept = {CONF_PARCEL_NO: "075624238061", "de_parcel_number": "YOXVB8CE"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="NL:1234AB",
        options={
            CONF_COUNTRY: "NL",
            CONF_POSTAL_CODE: "1234AB",
            CONF_PARCELS: [kept, {CONF_PARCEL_NO: "EXAMPLE222222"}],
        },
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["075624238061", "EXAMPLE111111"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        kept,
        {CONF_PARCEL_NO: "EXAMPLE111111"},
    ]


_PL_ACCOUNT_STATE = {
    "uid": "pl-user",
    "phone": "600000000",
    "refresh_token": "pl-refresh",
}
_PL_LOGIN = (
    "custom_components.gls.account.countries.pl.session.GlsPlSession.async_login"
)


async def _open_polish_account_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "pl"}
    )


async def test_polish_account_signs_in_with_phone(hass):
    result = await _open_polish_account_flow(hass)
    assert result["step_id"] == "pl"
    assert list(result["data_schema"].schema) == ["phone", "password"]
    with (
        patch(_PL_LOGIN, new=AsyncMock(return_value=_PL_ACCOUNT_STATE)),
        patch("custom_components.gls.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"phone": "600000000", "password": "private-password"},
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["title"] == "GLS Poland account"
    assert result["data"] == {"source": "account", **_PL_ACCOUNT_STATE}
    assert result["result"].unique_id == "account:pl-user"
    assert result["options"][CONF_COUNTRY] == "PL"
    assert "password" not in result["data"]


async def test_polish_account_sends_the_nine_digit_form_of_a_typed_number(hass):
    result = await _open_polish_account_flow(hass)
    with (
        patch(_PL_LOGIN, new=AsyncMock(return_value=_PL_ACCOUNT_STATE)) as login,
        patch("custom_components.gls.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"phone": "+48 600 000 000", "password": "private-password"},
        )
        await hass.async_block_till_done()
    login.assert_awaited_once_with("600000000", "private-password")
    assert result["type"] == "create_entry"


async def test_account_entry_stores_only_the_persisted_session_state(hass):
    result = await _open_polish_account_flow(hass)
    leaky = {**_PL_ACCOUNT_STATE, "access_token": "never-persisted"}
    with (
        patch(_PL_LOGIN, new=AsyncMock(return_value=leaky)),
        patch("custom_components.gls.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"phone": "600000000", "password": "private-password"},
        )
        await hass.async_block_till_done()
    assert result["data"] == {"source": "account", **_PL_ACCOUNT_STATE}


async def test_polish_account_rejects_a_number_that_is_not_nine_digits(hass):
    result = await _open_polish_account_flow(hass)
    with patch(_PL_LOGIN, new=AsyncMock()) as login:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "12345", "password": "private-password"}
        )
    login.assert_not_awaited()
    assert result["type"] == "form"
    assert result["errors"] == {"phone": "invalid_phone"}


async def test_polish_account_reauth_shows_phone_and_matches_identity(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:pl-user",
        data={"source": "account", **_PL_ACCOUNT_STATE},
        options={CONF_COUNTRY: "PL"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["identifier"] == "600000000"
    renewed = {**_PL_ACCOUNT_STATE, "refresh_token": "rotated"}
    with (
        patch(_PL_LOGIN, new=AsyncMock(return_value=renewed)) as login,
        patch.object(hass.config_entries, "async_reload", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-password"}
        )
        await hass.async_block_till_done()
    login.assert_awaited_once_with("600000000", "new-password")
    assert result["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "rotated"
