"""The account-country registry stays in step with the selectable countries."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gls.account.countries import ACCOUNT_SOURCES, account_country
from custom_components.gls.account.countries.dk import session as dk_session
from custom_components.gls.account.countries.dk.session import (
    GlsDkApiError,
    GlsDkAuthError,
)
from custom_components.gls.account.countries.pl import session as pl_session
from custom_components.gls.account.countries.pl.session import (
    GlsPlApiError,
    GlsPlAuthError,
)
from custom_components.gls.account.errors import GlsAccountApiError, GlsAccountAuthError
from custom_components.gls.config_flow import GlsConfigFlow
from custom_components.gls.const import CONF_COUNTRY, DOMAIN


def test_every_registry_row_is_consistent():
    for code, country in ACCOUNT_SOURCES.items():
        assert country.code == code
        # Entity IDs derive from the device name: never an identifier.
        assert country.device_name.startswith("GLS ")


def test_account_country_follows_the_entry_option():
    for code in ACCOUNT_SOURCES:
        entry = MockConfigEntry(
            domain=DOMAIN, data={"source": "account"}, options={CONF_COUNTRY: code}
        )
        assert account_country(entry) is ACCOUNT_SOURCES[code]


def test_account_country_has_no_silent_default():
    """Every account entry ever created stored its country; none is guessed."""
    entry = MockConfigEntry(domain=DOMAIN, data={"source": "account"})
    with pytest.raises(KeyError):
        account_country(entry)


def test_login_normalizers_keep_the_typed_form():
    assert ACCOUNT_SOURCES["DK"].normalize_login("  user@example.com ") == (
        "user@example.com"
    )
    assert ACCOUNT_SOURCES["DK"].normalize_login("   ") is None
    assert ACCOUNT_SOURCES["PL"].normalize_login("+48 600 000 000") == "600000000"


def test_country_errors_are_the_shared_account_errors():
    assert issubclass(GlsDkApiError, GlsAccountApiError)
    assert issubclass(GlsDkAuthError, GlsAccountAuthError)
    assert issubclass(GlsDkAuthError, GlsDkApiError)
    assert issubclass(GlsPlApiError, GlsAccountApiError)
    assert issubclass(GlsPlAuthError, GlsAccountAuthError)


def test_every_account_country_has_its_config_flow_step():
    """A registry row also needs its own named sign-in step for the form text."""
    for code in ACCOUNT_SOURCES:
        step = getattr(GlsConfigFlow, f"async_step_{code.lower()}", None)
        assert callable(step), f"no sign-in step for account country {code}"


def test_persisted_state_keeps_only_identity_token_and_sign_in_field():
    state = {
        "uid": "u",
        "refresh_token": "r",
        "phone": "600000000",
        "email": "not-polish@example.test",
        "access_token": "never",
        "password": "never",
    }
    assert ACCOUNT_SOURCES["PL"].persisted_state(state) == {
        "uid": "u",
        "refresh_token": "r",
        "phone": "600000000",
    }
    # An empty value never overwrites a stored one.
    assert ACCOUNT_SOURCES["DK"].persisted_state({"uid": "u", "email": ""}) == {
        "uid": "u"
    }


def test_login_field_is_the_session_modules_own_state_key():
    """The row and the session share one constant, so they cannot drift."""

    assert ACCOUNT_SOURCES["DK"].login_field == dk_session.LOGIN_FIELD
    assert ACCOUNT_SOURCES["PL"].login_field == pl_session.LOGIN_FIELD
    for country in ACCOUNT_SOURCES.values():
        session = country.create_session(None)
        assert country.login_field in session._state()
