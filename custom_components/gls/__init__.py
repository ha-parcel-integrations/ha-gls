"""GLS parcel tracker custom component for Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.coordinator import GlsAccountCoordinator
from .account.countries import AccountSession, account_country
from .const import (
    CONF_COUNTRY,
    CONF_DE_APP_INSTANCE_ID,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DOMAIN,
    PLATFORMS,
    is_account_entry,
)
from .contents import ContentsStore
from .coordinator import GlsBaseCoordinator
from .services import async_setup_services, async_unload_services
from .tracking.api import GlsApiClient
from .tracking.coordinator import GlsCoordinator
from .tracking.countries.de.session import GlsDeSession

_LOGGER = logging.getLogger(__name__)


@dataclass
class GlsData:
    """Runtime data attached to a GLS config entry."""

    client: GlsApiClient | AccountSession
    coordinator: GlsBaseCoordinator
    de_session: GlsDeSession | None = None
    # The options as last applied, so an entry write that only rotated DE
    # session state is not mistaken for an options edit.
    options: Mapping[str, object] = field(default_factory=dict)


type GlsConfigEntry = ConfigEntry[GlsData]


async def async_setup_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Set up GLS from a config entry."""
    if is_account_entry(entry):
        return await _async_setup_account(hass, entry)
    country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    postal_code = entry.options.get(CONF_POSTAL_CODE)

    # Older entries stored the postcode beside every parcel number.  A hub now
    # owns one postcode, so migrate the common single-postcode shape on setup.
    # Entries with mixed legacy postcodes keep their first postcode; users can
    # then create a second hub and move the relevant codes there.
    old_parcels = entry.options.get(CONF_PARCELS, [])
    legacy_postcode = postal_code or next(
        (
            parcel.get(CONF_POSTAL_CODE)
            for parcel in old_parcels
            if parcel.get(CONF_POSTAL_CODE)
        ),
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
    # f"{country}:{postal_code}": entries
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
    # guest-account session, created per install.
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
    await coordinator.contents_store.async_load()

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GlsData(
        client=client,
        coordinator=coordinator,
        de_session=de_session,
        options=dict(entry.options),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via
    # a coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_setup_account(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Restore an account session before loading the shared entity platforms."""
    country = account_country(entry)

    @callback
    def token_updated(state: dict) -> None:
        # A restored session never knows its sign-in identifier (it is learned
        # at sign-in only); ``persisted_state`` drops that empty value, so the
        # stored one is kept.
        data = {**entry.data, **country.persisted_state(state)}
        if data != entry.data:
            hass.config_entries.async_update_entry(entry, data=data)

    session = country.create_session(
        async_get_clientsession(hass), entry.data, token_updated
    )
    coordinator = GlsAccountCoordinator(hass, session, entry)
    await coordinator.contents_store.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = GlsData(
        client=session, coordinator=coordinator, options=dict(entry.options)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    async_setup_services(hass, hub=False)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: GlsConfigEntry) -> None:
    """Apply changed options by refreshing the coordinator.

    Every entry write notifies this listener, not just an options edit, so a
    write that only persisted session state (a new DE ``appInstanceId``, a
    rotated account token) used to poll as well — through
    ``async_request_refresh``, which bypasses HA's "Enable polling for
    updates" system option (that only gates the *scheduled* refresh).
    A newly learned ``parcelNumber`` does change the options and still
    refreshes, which is the behaviour ``_persist_de_state`` relies on.
    """
    data = entry.runtime_data
    if entry.options == data.options:
        return
    data.options = dict(entry.options)
    await data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> bool:
    """Unload a GLS config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # Drop a service when the last entry that needs it goes, so unloading one
    # entry never breaks the others.
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
    ]
    hubs_left = any(not is_account_entry(other) for other in others)
    async_unload_services(hass, hubs_left=hubs_left, entries_left=bool(others))
    return True


async def async_remove_entry(hass: HomeAssistant, entry: GlsConfigEntry) -> None:
    """Delete the contents the user set for the removed entry."""
    await ContentsStore(hass, entry.entry_id).async_remove()
