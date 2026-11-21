"""Config flow for the GLS parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.countries import ACCOUNT_SOURCES, AccountCountry, account_country
from .account.errors import GlsAccountApiError, GlsAccountAuthError
from .const import (
    CONF_ACCOUNT_UID,
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_SOURCE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    REQUEST_COUNTRY_URL,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
    is_account_entry,
)
from .tracking.countries.de.session import GlsDeSession, GlsDeSessionError

_LOGGER = logging.getLogger(__name__)

# First-run form: pick the delivery country and postcode. The postcode
# becomes the hub default, so adding a parcel later needs only its tracking
# number. The setup form links to the organisation discussion for requesting
# a country not yet in COUNTRIES (see REQUEST_COUNTRY_URL).
_COUNTRY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Selector option values double as hassfest translation keys, which
        # must be lowercase — COUNTRIES/CONF_COUNTRY's actual stored value
        # stays upper-case (DPD-style ``"NL"``/``"DE"``) everywhere else, so
        # this list is a display-only lowercase mirror. async_step_tracking
        # upper-cases the submitted value right back before using it.
        options=[code.lower() for code in COUNTRIES],
        translation_key=CONF_COUNTRY,
        mode=selector.SelectSelectorMode.DROPDOWN,
        # Alphabetise in the frontend, which sorts on the *translated* label.
        # Sorting the codes here would order by ISO code instead — DE before
        # DK puts Germany above Denmark — and even sorting English names would
        # be wrong for every other language the selector is translated into.
        sort=True,
    )
)

_ACCOUNT_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_COUNTRY, default=next(iter(ACCOUNT_SOURCES)).lower()
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[code.lower() for code in ACCOUNT_SOURCES],
                translation_key=CONF_COUNTRY,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }
)


_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def _hub_schema(country: str) -> vol.Schema:
    """Build the setup schema, defaulting the country to the last-picked one.

    A ``vol.Schema`` built once at import time would always default back to
    ``DEFAULT_COUNTRY`` when the form is re-shown after a validation error —
    the country the user picked would visibly reset to NL
    (ha-parcel-integrations/ha-gls#4). Rebuilding it per-request keeps the
    selection sticky across errors. ``country`` is the upper-case stored
    value; the selector itself only speaks lower-case (see
    ``_COUNTRY_SELECTOR``), hence the ``.lower()`` on the default.
    """
    return vol.Schema(
        {
            vol.Required(CONF_COUNTRY, default=country.lower()): _COUNTRY_SELECTOR,
            vol.Required(CONF_POSTAL_CODE): str,
        }
    )


def normalize_postcode(value: str) -> str:
    """Return the postcode without spaces and upper-cased (``1234AB``)."""
    return value.replace(" ", "").upper()


def normalize_parcel_no(value: str) -> str:
    """Return the parcel number/tracking ID trimmed and upper-cased.

    GLS tracking IDs are upper-case alphanumeric; upper-casing keeps the URL
    and the duplicate check consistent regardless of how the user typed it.
    """
    return value.strip().upper()


def valid_parcel_no(value: str) -> bool:
    """Accept every non-empty code; GLS gives out two differently-shaped identifiers."""
    return bool(value)


def valid_postcode(value: str, country: str) -> bool:
    """Whether ``value`` is a valid postcode for the given country."""
    cfg = COUNTRIES.get(country)
    if cfg is None:
        return False
    return bool(re.match(cfg["postcode_regex"], value))


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


class GlsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the GLS integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GlsOptionsFlowHandler:
        """Return the options flow handler for the entry's source."""
        if is_account_entry(config_entry):
            return GlsAccountOptionsFlowHandler()
        return GlsOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a GLS account and tracking numbers."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a GLS hub — one per delivery postal code.

        Multiple hubs are allowed (e.g. home + work); each is keyed on its
        postal code, so the same postcode can only be added once. The country
        picks the endpoint (host/culture) and the postcode format.
        """
        errors: dict[str, str] = {}
        country = DEFAULT_COUNTRY

        if user_input is not None:
            country = user_input[CONF_COUNTRY].upper()
            postal_code = normalize_postcode(user_input[CONF_POSTAL_CODE])
            if not valid_postcode(postal_code, country):
                errors[CONF_POSTAL_CODE] = "invalid_postcode"
            else:
                # unique_id is country-scoped so the same postcode string can
                # be a different hub in a different country (e.g. French and
                # German postcodes are both 5 digits) — see __init__.py's
                # migration for entries created before this scoping existed.
                await self.async_set_unique_id(f"{country}:{postal_code}")
                self._abort_if_unique_id_configured()

                # DE has no keyless route — mint the anonymous app-instance
                # identity now (guest-account) so the hub can poll from the moment it's created. The id goes in
                # entry.data, never entry.options: it isn't a user
                # preference, options get rewritten on every parcel
                # add/remove, and entry.data is what async_migrate_entry
                # already knows how to move.
                entry_data: dict[str, Any] = {CONF_SOURCE: SOURCE_TRACKING}
                if country == "DE":
                    de_session = GlsDeSession(async_get_clientsession(self.hass))
                    try:
                        entry_data[
                            CONF_DE_APP_INSTANCE_ID
                        ] = await de_session.async_register()
                    except (GlsDeSessionError, aiohttp.ClientError) as err:
                        _LOGGER.warning(
                            "GLS DE registration failed during setup: %s", err
                        )
                        errors["base"] = "de_registration_failed"

                if not errors:
                    return self.async_create_entry(
                        title=f"GLS ({postal_code})",
                        data=entry_data,
                        options={
                            CONF_COUNTRY: country,
                            CONF_PARCELS: [],
                            CONF_POSTAL_CODE: postal_code,
                            CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                            CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                            CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                        },
                    )

        return self.async_show_form(
            step_id=SOURCE_TRACKING,
            data_schema=_hub_schema(country),
            errors=errors,
            description_placeholders={
                "issue_url": REQUEST_COUNTRY_URL,
                "postcode_example": COUNTRIES[country]["postcode_example"],
            },
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the country of the GLS account, then sign in to it."""
        if user_input is not None:
            # One step per account country, so each keeps its own form text.
            return await getattr(self, f"async_step_{user_input[CONF_COUNTRY]}")()
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=_ACCOUNT_SCHEMA,
            description_placeholders={"issue_url": REQUEST_COUNTRY_URL},
        )

    async def async_step_dk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate an existing Danish GLS account without retaining its password."""
        return await self._async_account_sign_in(ACCOUNT_SOURCES["DK"], user_input)

    async def async_step_pl(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate an existing Polish GLS account without retaining its password."""
        return await self._async_account_sign_in(ACCOUNT_SOURCES["PL"], user_input)

    async def _async_account_sign_in(
        self, country: AccountCountry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Sign in to ``country``'s account and create its entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            identifier = country.normalize_login(user_input[country.login_field])
            if identifier is None:
                errors[country.login_field] = f"invalid_{country.login_field}"
            else:
                result = await self._async_account_create(
                    country, identifier, user_input[CONF_PASSWORD], errors
                )
                if result is not None:
                    return result
        return self.async_show_form(
            step_id=country.code.lower(),
            data_schema=vol.Schema(
                {
                    vol.Required(country.login_field): selector.TextSelector(
                        selector.TextSelectorConfig(type=country.login_input_type)
                    ),
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def _async_account_create(
        self,
        country: AccountCountry,
        identifier: str,
        password: str,
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        """Sign in and create the entry; on failure fill ``errors``, return None."""
        state = await self._async_account_login(country, identifier, password, errors)
        if state is None:
            return None
        await self.async_set_unique_id(f"{SOURCE_ACCOUNT}:{state[CONF_ACCOUNT_UID]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=country.title,
            data={CONF_SOURCE: SOURCE_ACCOUNT, **state},
            options={
                CONF_COUNTRY: country.code,
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def _async_account_login(
        self,
        country: AccountCountry,
        identifier: str,
        password: str,
        errors: dict[str, str],
    ) -> dict[str, str] | None:
        """Sign in; return the state to persist, or fill ``errors`` and None."""
        session = country.create_session(async_get_clientsession(self.hass))
        try:
            state = await session.async_login(identifier, password)
        except GlsAccountAuthError:
            errors["base"] = "invalid_auth"
            return None
        except (GlsAccountApiError, aiohttp.ClientError):
            errors["base"] = "cannot_connect"
            return None
        return country.persisted_state(state)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Reauthenticate only an existing account entry."""
        if not is_account_entry(self._get_reauth_entry()):
            return self.async_abort(reason="reauth_not_supported")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the stored account's password; never silently change accounts."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        country = account_country(entry)
        identifier = entry.data[country.login_field]
        if user_input is not None:
            state = await self._async_account_login(
                country, identifier, user_input[CONF_PASSWORD], errors
            )
            if state is not None:
                await self.async_set_unique_id(
                    f"{SOURCE_ACCOUNT}:{state[CONF_ACCOUNT_UID]}"
                )
                self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")
                return self.async_update_reload_and_abort(entry, data_updates=state)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            description_placeholders={"identifier": identifier},
            errors=errors,
        )


class GlsOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels and settings via a two-page menu.

    ``async_step_init`` shows a menu (``parcels`` / ``settings``) rather than
    one long sectioned form. The ``parcels`` page edits the whole tracked-code
    list at once; ``settings`` holds delivered-parcel retention and history.
    Polling cadence is not configurable — the coordinator drives it from what
    the tracked parcels are doing. Adding a parcel needs only its number — the postcode is
    inherited from the hub. Changes apply live via HA's options-update
    listener (which refreshes the coordinator), so new/removed per-parcel
    sensors appear and disappear immediately.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init", menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    normalize_parcel_no(code)
                    for code in user_input.get("tracking_codes", [])
                    if normalize_parcel_no(code)
                )
            )
            if any(not valid_parcel_no(code) for code in codes):
                errors["base"] = "invalid_parcel_no"
            if not errors:
                # Kept codes keep their stored item: Germany's learned
                # parcelNumber lives there and cannot be recovered on its own.
                existing = {
                    item[CONF_PARCEL_NO]: item
                    for item in _current_parcels(self.config_entry)
                }
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [
                            existing.get(code, {CONF_PARCEL_NO: code}) for code in codes
                        ],
                    },
                )
        current_codes = [
            parcel[CONF_PARCEL_NO] for parcel in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INCLUDE_HISTORY,
                        default=current.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )


class GlsAccountOptionsFlowHandler(GlsOptionsFlowHandler):
    """Settings for an account entry; its parcels come from the account."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Skip the manual menu: an account has settings only."""
        return await self.async_step_settings(user_input)
