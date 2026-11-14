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
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DK_EMAIL,
    CONF_DK_REFRESH_TOKEN,
    CONF_DK_UID,
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_SOURCE,
    CONF_USE_DANISH_ACCOUNT,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    REQUEST_COUNTRY_URL,
    SOURCE_DK_ACCOUNT,
)
from .countries.de.session import GlsDeSession, GlsDeSessionError
from .countries.dk.session import GlsDkApiError, GlsDkAuthError, GlsDkSession

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
        # this list is a display-only lowercase mirror. async_step_user
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
            vol.Optional(CONF_POSTAL_CODE): str,
            vol.Optional(CONF_USE_DANISH_ACCOUNT, default=False): bool,
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
        if config_entry.data.get(CONF_SOURCE) == SOURCE_DK_ACCOUNT:
            return GlsAccountOptionsFlowHandler()
        return GlsOptionsFlowHandler()

    async def async_step_user(
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
            if user_input.get(CONF_USE_DANISH_ACCOUNT):
                return await self.async_step_dk_account()
            country = user_input[CONF_COUNTRY].upper()
            postal_code = normalize_postcode(user_input.get(CONF_POSTAL_CODE, ""))
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
                entry_data: dict[str, Any] = {}
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
            step_id="user",
            data_schema=_hub_schema(country),
            errors=errors,
            description_placeholders={
                "issue_url": REQUEST_COUNTRY_URL,
                "postcode_example": COUNTRIES[country]["postcode_example"],
            },
        )

    async def async_step_dk_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate an existing Danish GLS account without retaining its password."""
        return await self._async_account_form(user_input, reauth=False)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Reauthenticate only an existing account entry."""
        if entry_data.get(CONF_SOURCE) != SOURCE_DK_ACCOUNT:
            return self.async_abort(reason="reauth_not_supported")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require credentials for the same account before resuming its session."""
        return await self._async_account_form(user_input, reauth=True)

    async def _async_account_form(
        self, user_input: dict[str, Any] | None, *, reauth: bool
    ) -> ConfigFlowResult:
        """Share credential validation between initial setup and HA reauth."""
        errors: dict[str, str] = {}
        email = ""
        entry = self._get_reauth_entry() if reauth else None
        if entry is not None:
            email = entry.data.get(CONF_DK_EMAIL, "")
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            session = GlsDkSession(async_get_clientsession(self.hass))
            try:
                state = await session.async_login(email, user_input[CONF_PASSWORD])
            except GlsDkAuthError:
                errors["base"] = "invalid_auth"
            except (GlsDkApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                uid = state["uid"]
                if entry is not None:
                    if uid != entry.data.get(CONF_DK_UID):
                        return self.async_abort(reason="reauth_account_mismatch")
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_DK_UID: uid,
                            CONF_DK_EMAIL: state["email"],
                            CONF_DK_REFRESH_TOKEN: state["refresh_token"],
                        },
                    )
                await self.async_set_unique_id(f"{SOURCE_DK_ACCOUNT}:{uid}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="GLS Denmark account",
                    data={
                        CONF_SOURCE: SOURCE_DK_ACCOUNT,
                        CONF_DK_UID: uid,
                        CONF_DK_EMAIL: state["email"],
                        CONF_DK_REFRESH_TOKEN: state["refresh_token"],
                    },
                    options={
                        CONF_COUNTRY: "DK",
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm" if reauth else "dk_account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=email): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.EMAIL
                        )
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
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
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [{CONF_PARCEL_NO: code} for code in codes],
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


class GlsAccountOptionsFlowHandler(OptionsFlowWithReload, GlsOptionsFlowHandler):
    """Settings for a Danish account entry; its parcels come from the account.

    Saving changed options reloads the entry, so an account entry registers no
    update listener: ``OptionsFlowWithReload`` forbids combining the two.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Skip the manual menu: an account has settings only."""
        return await self.async_step_settings(user_input)
