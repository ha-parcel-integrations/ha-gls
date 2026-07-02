"""Config flow for the GLS parcel tracker integration."""
from __future__ import annotations

import logging
import re
from typing import Any

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

from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCEL_NO,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# A parcel can be tracked by either identifier GLS gives out: the long
# numeric parcel number (e.g. 13290054100304) or the short alphanumeric
# tracking ID / uniqueNo (e.g. 00L1B3BX). Both resolve on the endpoint, so
# accept letters and digits.
_PARCEL_NO_RE = re.compile(r"^[A-Z0-9]{6,20}$")
_POSTCODE_RE = re.compile(r"^\d{4}[A-Z]{2}$")

# First-run form: only ask for the delivery postal code. It becomes the hub
# default, so adding a parcel later needs only its tracking number.
_HUB_SCHEMA = vol.Schema({vol.Required(CONF_POSTAL_CODE): str})


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


def valid_postcode(value: str) -> bool:
    """Whether ``value`` is a Dutch postcode (``1234AB``)."""
    return bool(_POSTCODE_RE.match(value))


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _interval_selector() -> selector.SelectSelector:
    """The refresh-interval dropdown selector."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=str(m), label=f"{m} minutes")
                for m in REFRESH_INTERVAL_OPTIONS
            ],
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
        """Create the GLS hub — only the delivery postal code is needed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            postal_code = normalize_postcode(user_input[CONF_POSTAL_CODE])
            if not valid_postcode(postal_code):
                errors[CONF_POSTAL_CODE] = "invalid_postcode"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="GLS",
                    data={},
                    options={
                        CONF_PARCELS: [],
                        CONF_POSTAL_CODE: postal_code,
                        CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_HUB_SCHEMA, errors=errors
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
                        CONF_POSTAL_CODE: hub_postcode,
                        CONF_PARCELS: parcels,
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
