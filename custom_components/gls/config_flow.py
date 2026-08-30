"""Config flow for the GLS parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
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
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_NEW_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_AUTO,
    REFRESH_INTERVAL_OPTIONS,
    REQUEST_COUNTRY_URL,
)
from .countries.de.session import GlsDeSession, GlsDeSessionError

_LOGGER = logging.getLogger(__name__)

# A parcel can be tracked by either identifier GLS gives out: the long
# numeric parcel number (e.g. 13290054100304) or the short alphanumeric
# tracking ID / uniqueNo (e.g. 00L1B3BX). Both resolve on the endpoint, so
# accept letters and digits.
_PARCEL_NO_RE = re.compile(r"^[A-Z0-9]{6,20}$")

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
    """Whether ``value`` looks like a GLS parcel number or tracking ID."""
    return bool(_PARCEL_NO_RE.match(value))


def valid_postcode(value: str, country: str) -> bool:
    """Whether ``value`` is a valid postcode for the given country."""
    cfg = COUNTRIES.get(country)
    if cfg is None:
        return False
    return bool(re.match(cfg["postcode_regex"], value))


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[REFRESH_INTERVAL_AUTO] + [str(m) for m in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class GlsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the GLS integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GlsOptionsFlowHandler:
        """Return the options flow handler."""
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
                # identity now (guest-account, BUILD_PLAN_DE.md §3) so the
                # hub can poll from the moment it's created. The id goes in
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
                            # New hubs default to dynamic polling; a hub set
                            # up before this option existed keeps reading
                            # DEFAULT_REFRESH_INTERVAL via the coordinator's
                            # .get() fallback instead (dynamic-polling.md
                            # Section 5.2).
                            CONF_REFRESH_INTERVAL: DEFAULT_NEW_REFRESH_INTERVAL,
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


class GlsOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels, history and polling in one sectioned form.

    Mirrors the other suite carriers' section layout (here: ``parcels`` /
    ``history`` / ``polling``). Adding a parcel needs only its number — the
    postcode is inherited from the hub. Changes apply live via HA's
    options-update listener (which refreshes the coordinator), so new/removed
    per-parcel sensors appear and disappear immediately.
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
                    CONF_REFRESH_INTERVAL: (
                        REFRESH_INTERVAL_AUTO
                        if user_input[CONF_REFRESH_INTERVAL] == REFRESH_INTERVAL_AUTO
                        else int(user_input[CONF_REFRESH_INTERVAL])
                    ),
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
                    vol.Required(
                        CONF_REFRESH_INTERVAL,
                        default=str(
                            current.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)
                        ),
                    ): _interval_selector(),
                }
            ),
        )
