"""GLS parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GlsApiClient
from .const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GlsCoordinator, _refresh_interval
from .countries.de.session import GlsDeSession
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class GlsData:
    """Runtime data attached to a GLS config entry."""

    client: GlsApiClient
    coordinator: GlsCoordinator
    de_session: GlsDeSession | None = None


type GlsConfigEntry = ConfigEntry[GlsData]


async def async_setup_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Set up GLS from a config entry."""
    country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    postal_code = entry.options.get(CONF_POSTAL_CODE)

    # Older entries stored the postcode beside every parcel number.  A hub now
    # owns one postcode, so migrate the common single-postcode shape on setup.
    # Entries with mixed legacy postcodes keep their first postcode; users can
    # then create a second hub and move the relevant codes there.
    old_parcels = entry.options.get(CONF_PARCELS, [])
    legacy_postcode = postal_code or next(
        (parcel.get(CONF_POSTAL_CODE) for parcel in old_parcels if parcel.get(CONF_POSTAL_CODE)),
        None,
    )
    normalized_parcels = [
        {key: value for key, value in parcel.items() if key != CONF_POSTAL_CODE}
        for parcel in old_parcels
    ]
    if legacy_postcode and (
        legacy_postcode != postal_code or normalized_parcels != old_parcels
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_POSTAL_CODE: legacy_postcode,
                CONF_PARCELS: normalized_parcels,
            },
        )
        postal_code = legacy_postcode

    # unique_id migrations, oldest scheme first, both converging on today's
    # f"{country}:{postal_code}" (BUILD_PLAN_GROUP_COUNTRIES.md §4): entries
    # from before the multi-hub redesign carry unique_id == DOMAIN, and
    # entries from 1.5.1 and earlier carry a bare postcode. Six new countries
    # made a bare-postcode collision ordinary rather than theoretical (French
    # and German postcodes can both read "39100"), so a hub's country is now
    # part of its identity. A legacy DOMAIN entry converges in one hop
    # instead of two. Do NOT drop or reorder this without re-running the
    # migration test — it must never orphan a production hub.
    if postal_code and entry.unique_id in (DOMAIN, postal_code):
        hass.config_entries.async_update_entry(
            entry, unique_id=f"{country}:{postal_code}"
        )

    # The endpoint host + culture come from the hub country; entries created
    # before the country option default to the Netherlands. NL is keyless
    # (the HA-managed session is enough); DE needs its own anonymous
    # guest-account session (BUILD_PLAN_DE.md §3) — self-minted, not shared,
    # so it still clears the shared-secret refusal.
    country_cfg = COUNTRIES.get(country, COUNTRIES[DEFAULT_COUNTRY])

    de_session: GlsDeSession | None = None
    if country == "DE":
        de_session = GlsDeSession(
            async_get_clientsession(hass),
            app_instance_id=entry.data.get(CONF_DE_APP_INSTANCE_ID),
        )

    client = GlsApiClient(
        async_get_clientsession(hass),
        host=country_cfg["host"],
        # CZ has no "culture" key (it uses "group_locale" instead — see the
        # COUNTRIES docstring), so this defaults to "" rather than KeyError.
        culture=country_cfg.get("culture", ""),
        country=country,
        de_session=de_session,
        group_locale=country_cfg.get("group_locale"),
    )
    coordinator = GlsCoordinator(hass, client, entry, de_session=de_session)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GlsData(client=client, coordinator=coordinator, de_session=de_session)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, interval, history) live via
    # a coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: GlsConfigEntry) -> None:
    """Apply changed options: retune the interval and refresh the coordinator."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _refresh_interval(entry)
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Unload a GLS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared across hubs, so only remove them once the last
    # hub is gone — otherwise unloading one hub would break the others.
    others_loaded = any(
        other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not others_loaded:
        async_unload_services(hass)
    return True
