"""Automatic parcel discovery for a Danish GLS account."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import MID_INTERVAL_MINUTES
from .coordinator import GlsCoordinator
from .countries.dk import normalize_parcel_dk
from .countries.dk.session import GlsDkApiError, GlsDkAuthError, GlsDkSession


class GlsAccountCoordinator(GlsCoordinator):
    """Poll a complete account feed while reusing the shared entity lifecycle."""

    def __init__(
        self, hass: HomeAssistant, session: GlsDkSession, entry: ConfigEntry
    ) -> None:
        """Initialize account discovery without a manual tracking client."""
        super().__init__(hass, None, entry)
        self._account_session = session

    async def _async_poll(self) -> list[dict]:
        """Validate the full feed before changing entities or firing events."""
        try:
            feed = await self._account_session.async_get_parcels()
        except GlsDkAuthError as err:
            raise ConfigEntryAuthFailed("GLS account requires sign-in") from err
        except GlsDkApiError as err:
            raise UpdateFailed(
                "GLS account could not be refreshed",
                retry_after=(err.retry_after or 3600)
                if err.status_code == 429
                else None,
            ) from err

        if not isinstance(feed, dict):
            raise UpdateFailed("GLS account returned an invalid parcel list")
        items, count = feed.get("items"), feed.get("totalCount")
        if (
            not isinstance(items, list)
            or type(count) is not int
            or count < 0
            or count != len(items)
        ):
            raise UpdateFailed("GLS account returned an incomplete parcel list")
        entries: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise UpdateFailed("GLS account returned an invalid parcel")
            number = item.get("parcelNumber")
            if (
                not isinstance(number, str)
                or not number.strip()
                or number.strip() in seen
                or item.get("userId") != self._account_session.uid
            ):
                raise UpdateFailed("GLS account parcel identity could not be verified")
            seen.add(number.strip())
            entries.append(
                (
                    number.strip(),
                    normalize_parcel_dk(item, include_history=self._include_history),
                )
            )
        return self._finish_update(
            entries, successful=True, discovery_minutes=MID_INTERVAL_MINUTES
        )
