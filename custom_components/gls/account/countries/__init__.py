"""Per-country GLS account logic: sign-in session and ``normalize_parcel_<code>``.

``ACCOUNT_SOURCES`` is the one place the shared account code (setup, config
flow, coordinator, device) learns what differs between account countries, so
none of those modules branches on a country code. Add a row here (its
position is the selector order), plus the config flow's one-line
``async_step_<code>`` delegate, for each new account country.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.selector import TextSelectorType

from ...const import CONF_ACCOUNT_REFRESH_TOKEN, CONF_ACCOUNT_UID, CONF_COUNTRY
from .dk import normalize_email_dk, normalize_parcel_dk, parse_feed_dk
from .dk.session import LOGIN_FIELD as DK_LOGIN_FIELD, GlsDkSession
from .pl import normalize_parcel_pl_account, normalize_phone_pl, parse_feed_pl
from .pl.session import LOGIN_FIELD as PL_LOGIN_FIELD, GlsPlSession


class AccountSession(Protocol):
    """What the shared account code needs from a country's session."""

    uid: str | None

    async def async_login(self, identifier: str, password: str) -> dict[str, str]:
        """Sign in and return the state to persist (it includes ``uid``)."""

    async def async_get_parcels(self) -> Any:
        """Fetch the account's raw feed for ``parse_feed``."""


class AccountSessionClass(Protocol):
    """The constructor every country's session class shares.

    ``AccountCountry.create_session`` calls exactly this signature, so a
    session class whose constructor drifts fails type checking, not at runtime.
    """

    def __call__(
        self,
        session: aiohttp.ClientSession,
        *,
        uid: str | None = None,
        refresh_token: str | None = None,
        token_updated: Callable[[dict[str, str]], None] | None = None,
    ) -> AccountSession:
        """Build a session around the shared client and stored credentials."""


@dataclass(frozen=True, kw_only=True)
class AccountCountry:
    """Everything that differs between account countries."""

    code: str
    # The entry title and the device name. Entity IDs derive from the device
    # name, so neither may ever carry the account's email or phone number.
    title: str
    device_name: str
    # The ``entry.data`` key holding the sign-in identifier reauth reuses — the
    # session module's own ``LOGIN_FIELD``, so its state and this row cannot
    # drift — the input type of its form field, and the canonical form it is
    # sent and stored in (``None`` rejects it with ``invalid_<login_field>``).
    login_field: str
    login_input_type: TextSelectorType
    normalize_login: Callable[[str], str | None]
    # Built with the shared client and the restored ``uid``/refresh token.
    session_class: AccountSessionClass
    parse_feed: Callable[[Any, str | None], list[tuple[str, dict]]]
    normalize: Callable[..., dict]

    def create_session(
        self,
        clientsession: aiohttp.ClientSession,
        data: Mapping[str, Any] | None = None,
        token_updated: Callable[[dict[str, str]], None] | None = None,
    ) -> AccountSession:
        """Build this country's session — fresh, or restored from entry ``data``.

        Without ``data`` it is a blank session for a sign-in (setup, reauth).
        With it, ``uid`` and the refresh token are restored; the sign-in
        identifier never is — a session learns it at sign-in only, and
        ``persisted_state`` never lets the empty value overwrite a stored one.
        """
        stored = data or {}
        return self.session_class(
            clientsession,
            uid=stored.get(CONF_ACCOUNT_UID),
            refresh_token=stored.get(CONF_ACCOUNT_REFRESH_TOKEN),
            token_updated=token_updated,
        )

    def persisted_state(self, state: Mapping[str, str]) -> dict[str, str]:
        """The only session state an entry may store, empty values dropped.

        Identity, refresh token and this country's sign-in field — whatever
        else a session's state grows. Every entry write goes through here.
        """
        keys = (CONF_ACCOUNT_UID, CONF_ACCOUNT_REFRESH_TOKEN, self.login_field)
        return {key: state[key] for key in keys if state.get(key)}


ACCOUNT_SOURCES: dict[str, AccountCountry] = {
    "DK": AccountCountry(
        code="DK",
        title="GLS Denmark account",
        device_name="GLS Denmark",
        login_field=DK_LOGIN_FIELD,
        login_input_type=TextSelectorType.EMAIL,
        normalize_login=normalize_email_dk,
        session_class=GlsDkSession,
        parse_feed=parse_feed_dk,
        normalize=normalize_parcel_dk,
    ),
    "PL": AccountCountry(
        code="PL",
        title="GLS Poland account",
        device_name="GLS Poland",
        login_field=PL_LOGIN_FIELD,
        login_input_type=TextSelectorType.TEL,
        normalize_login=normalize_phone_pl,
        session_class=GlsPlSession,
        parse_feed=parse_feed_pl,
        normalize=normalize_parcel_pl_account,
    ),
}


def account_country(entry: ConfigEntry) -> AccountCountry:
    """Return the account country of ``entry``.

    Every account entry ever created stored ``CONF_COUNTRY`` in its options
    (the released Danish flow wrote ``"DK"``), so there is no default.
    """
    return ACCOUNT_SOURCES[entry.options[CONF_COUNTRY]]
