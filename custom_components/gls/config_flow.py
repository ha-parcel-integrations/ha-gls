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
from homeassistant.data_entry_flow import section
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
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
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
        options=list(COUNTRIES),
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
    selection sticky across errors.
    """
    return vol.Schema(
        {
            vol.Required(CONF_COUNTRY, default=country): _COUNTRY_SELECTOR,
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
            options=[str(m) for m in REFRESH_INTERVAL_OPTIONS],
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
            country = user_input[CONF_COUNTRY]
            postal_code = normalize_postcode(user_input[CONF_POSTAL_CODE])
            if not valid_postcode(postal_code, country):
                errors[CONF_POSTAL_CODE] = "invalid_postcode"
            else:
                # unique_id is the postcode: fine while only NL is supported.
                # Add the country here once a second country lands.
                await self.async_set_unique_id(postal_code)
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
                        entry_data[CONF_DE_APP_INSTANCE_ID] = (
                            await de_session.async_register()
                        )
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
                            CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
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
        """Show and handle the single sectioned options form."""
        errors: dict[str, str] = {}
        parcels = _current_parcels(self.config_entry)
        hub_postcode = self.config_entry.options.get(CONF_POSTAL_CODE, "")

        if user_input is not None:
            parcels_section = user_input.get("parcels", {})
            delivered_section = user_input.get("delivered", {})
            history_section = user_input.get("history", {})
            polling_section = user_input.get("polling", {})

            # Remove first, then add — so re-adding a just-removed number works.
            to_remove = set(parcels_section.get("remove", []))
            parcels = [p for p in parcels if p[CONF_PARCEL_NO] not in to_remove]

            add_no = normalize_parcel_no(parcels_section.get("add") or "")
            if add_no:
                if not valid_parcel_no(add_no):
                    errors["base"] = "invalid_parcel_no"
                elif any(p[CONF_PARCEL_NO] == add_no for p in parcels):
                    errors["base"] = "already_tracked"
                else:
                    parcels.append(
                        {CONF_PARCEL_NO: add_no, CONF_POSTAL_CODE: hub_postcode}
                    )

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        # An options flow's `data` replaces `entry.options`
                        # wholesale rather than merging into it — omitting
                        # the country here silently reset every hub back to
                        # NL the first time a parcel was added
                        # (ha-parcel-integrations/ha-gls#2).
                        CONF_COUNTRY: self.config_entry.options.get(
                            CONF_COUNTRY, DEFAULT_COUNTRY
                        ),
                        CONF_POSTAL_CODE: hub_postcode,
                        CONF_PARCELS: parcels,
                        CONF_DELIVERED_FILTER_TYPE: delivered_section[
                            CONF_DELIVERED_FILTER_TYPE
                        ],
                        CONF_DELIVERED_FILTER_AMOUNT: int(
                            delivered_section[CONF_DELIVERED_FILTER_AMOUNT]
                        ),
                        CONF_INCLUDE_HISTORY: bool(
                            history_section[CONF_INCLUDE_HISTORY]
                        ),
                        CONF_REFRESH_INTERVAL: int(
                            polling_section[CONF_REFRESH_INTERVAL]
                        ),
                    },
                )

        current = self.config_entry.options

        parcels_fields: dict[Any, Any] = {vol.Optional("add", default=""): str}
        if parcels:
            parcels_fields[vol.Optional("remove", default=[])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=p[CONF_PARCEL_NO],
                            label=f"{p[CONF_PARCEL_NO]} ({p[CONF_POSTAL_CODE]})",
                        )
                        for p in parcels
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )

        schema = vol.Schema(
            {
                vol.Required("parcels"): section(
                    vol.Schema(parcels_fields), {"collapsed": False}
                ),
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
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
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so a
                                # stored int default trips "expected str" on submit.
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
