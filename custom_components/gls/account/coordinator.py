"""Automatic parcel discovery for a GLS account, whatever its country."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import MID_INTERVAL_MINUTES
from ..coordinator import GlsBaseCoordinator
from .countries import AccountSession, account_country
from .errors import GlsAccountApiError, GlsAccountAuthError, GlsAccountFeedError


class GlsAccountCoordinator(GlsBaseCoordinator):
    """Poll a complete account feed while reusing the shared entity lifecycle."""

    def __init__(
        self, hass: HomeAssistant, session: AccountSession, entry: ConfigEntry
    ) -> None:
        """Initialize account discovery."""
        super().__init__(hass, entry)
        self._account_session = session
        self._country = account_country(entry)

    async def _async_poll(self) -> list[dict]:
        """Validate the full feed before changing entities or firing events."""
        try:
            feed = await self._account_session.async_get_parcels()
        except GlsAccountAuthError as err:
            raise ConfigEntryAuthFailed("GLS account requires sign-in") from err
        except GlsAccountApiError as err:
            # ``GlsAccountApiError``'s contract: fixed text and a status code,
            # never a remote value — so both are safe to surface, and without
            # them HA's one-line "Error fetching" log hides which step failed.
            status = f" (HTTP {err.status_code})" if err.status_code else ""
            raise UpdateFailed(
                f"GLS account could not be refreshed: {err}{status}",
                retry_after=(err.retry_after or 3600)
                if err.status_code == 429
                else None,
            ) from err
        try:
            items = self._country.parse_feed(feed, self._account_session.uid)
        except GlsAccountFeedError as err:
            raise UpdateFailed(str(err)) from err
        entries = [
            (
                number,
                self._country.normalize(raw, include_history=self._include_history),
            )
            for number, raw in items
        ]
        return self._finish_update(
            entries, successful=True, discovery_minutes=MID_INTERVAL_MINUTES
        )
