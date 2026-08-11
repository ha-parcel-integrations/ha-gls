"""The GLS Germany anonymous app-instance identity: register, validate, refresh.

GLS Germany has no consumer account and no keyless route either — every route
on the parcel backend requires a ``Authorization: Bearer`` token. What makes
it buildable at all is that the token is **self-minted**: an integration
generates its own ``appInstanceId`` and exchanges it for a token nobody had
to hand us. That is a ``guest-account`` credential (a credential with a
lifecycle), not ``auth: none`` — see
``carrier-research/api/gls/germany.md``.

There is exactly one unauthenticated route in the whole DE surface,
``/users/validate`` — the decompiled app's own auth interceptor hardcodes
this single exception. Everything in this module is built around that one
exception:

- **At config-entry creation**, ``async_register()`` mints a fresh
  ``appInstanceId`` and stores it in ``entry.data`` (the caller's job, not
  this module's — see BUILD_PLAN_DE.md §3's "where the appInstanceId lives").
- **Before every poll**, ``async_get_token()`` refreshes the cached token via
  ``validate`` once it is within :data:`TOKEN_REFRESH_MARGIN` of expiry — a
  generous 6h, not 60s, because HA restarts and sleeps.
- **On a 401** from the parcel backend, ``async_handle_unauthorized()``
  forces exactly one refresh; the caller retries the failing request once
  and then gives up. This module never loops on its own.
- **On a 404 from validate**, the anonymous instance is gone server-side:
  this module re-registers a new one automatically, logs a WARNING (twice —
  once for the 404 itself, once for the successful recovery, per
  BUILD_PLAN_DE.md §6's "the identity" row), and sets :attr:`reregistered`
  so the caller knows to re-``POST`` every tracked parcel (the carrier-side
  list was reset, the user's tracked list was not).

Never send the token this module hands out to any host other than
``*.ooh.glsnxt.com`` / ``api-backend.glsnxt.com`` — that is the caller's
(the DE transport module's) responsibility, not enforced here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiohttp

from ...const import GLS_DE_REGISTER_URL, GLS_DE_VALIDATE_URL

_LOGGER = logging.getLogger(__name__)

# Re-bound under their historical local names: const.py is the single source
# of truth for these URLs (shared with `countries/de/__init__.py`'s
# `GLS_DE_TRACKING*` routes), but every reference in this module (and its
# tests) still reads `REGISTER_URL`/`VALIDATE_URL`.
REGISTER_URL = GLS_DE_REGISTER_URL
VALIDATE_URL = GLS_DE_VALIDATE_URL

# Refresh a cached token this long before it actually expires. Deliberately
# generous — HA restarts and sleeps mean "expires in 60s" is not a safe
# margin (BUILD_PLAN_DE.md §3.2).
TOKEN_REFRESH_MARGIN = timedelta(hours=6)

# The token's observed lifetime on 2026-08-10 was ~60h (`exp - iat` ≈
# 216 000s). Used as the fallback expiry when a response's own
# issuedAt/expiresAt can't be parsed, and as the baseline for the "materially
# different" anomaly warning (§6).
_EXPECTED_TOKEN_LIFETIME = timedelta(hours=60)
_TOKEN_LIFETIME_TOLERANCE = timedelta(hours=12)

_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-gls/issues/new"
    "?template=unrecognised_status.yml"
)

# One-shot per HA session, keyed like `parcels.py`'s `_unmapped_states_logged`
# so distinct anomalous lifetimes each get their own warning instead of only
# the first one ever seen.
_token_lifetime_warned_hours: set[int] = set()
_recaptcha_warned = False


class GlsDeSessionError(Exception):
    """Raised when the identity service can't hand out a usable token."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Store the message and, where available, the HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


class _InstanceGoneError(Exception):
    """Internal signal: ``validate`` answered 404 — the instance is gone."""


def _parse_identity_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of a register/validate ``issuedAt``/``expiresAt``.

    The exact wire format was never captured — the gate blocked before a
    build was written against a real response's identity payload. The field
    names mirror the JWT's own ``iat``/``exp`` claims, so Unix-epoch seconds
    is the primary guess; an ISO 8601 string is accepted as a fallback.
    Returns ``None`` on anything else so callers fall back to the expected
    ~60h lifetime instead of crashing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _warn_token_lifetime_anomaly(lifetime: timedelta) -> None:
    """One-shot-per-value warning when a token's lifetime is far from ~60h (§6)."""
    hours = round(lifetime.total_seconds() / 3600)
    if hours in _token_lifetime_warned_hours:
        return
    _token_lifetime_warned_hours.add(hours)
    _LOGGER.warning(
        "GLS Germany issued a token with a %sh lifetime — expected ~60h. "
        "The identity service's contract may have changed. Open an issue "
        "and paste this line: %s\n  observed_lifetime_hours=%s",
        hours,
        _NEW_ISSUE_URL,
        hours,
    )


def _warn_recaptcha_once() -> None:
    """One-shot, loud warning: any mention of reCAPTCHA means the surface may close."""
    global _recaptcha_warned
    if _recaptcha_warned:
        return
    _recaptcha_warned = True
    _LOGGER.warning(
        "GLS Germany's identity service response mentions reCAPTCHA — the "
        "guest-account surface may be closing. Open an issue: %s",
        _NEW_ISSUE_URL,
    )


def _mentions_recaptcha(text: str, headers: Any) -> bool:
    """Whether a response body or headers mention reCAPTCHA."""
    if "recaptcha" in text.lower():
        return True
    return any(str(key).lower() == "x-recaptcha-token" for key in headers)


class GlsDeSession:
    """Owns one config entry's anonymous ``appInstanceId`` + bearer token.

    One instance per config entry, constructed with the entry's shared
    aiohttp session and (once past config-flow) the ``appInstanceId``
    persisted in ``entry.data``. A fresh instance always starts with no
    cached token, so its first :meth:`async_get_token` call refreshes via
    ``validate`` even though the id itself is not new.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        app_instance_id: str | None = None,
    ) -> None:
        """Initialise with an optional already-persisted ``appInstanceId``.

        ``app_instance_id=None`` means this config entry has never
        registered before; call :meth:`async_register` to mint one.
        """
        self._session = session
        self.app_instance_id = app_instance_id
        self._token: str | None = None
        self._expires_at: datetime | None = None
        # Set once a recovery re-register happens, cleared by
        # `pop_reregistered()` — the coordinator's signal to re-POST every
        # tracked parcel because the carrier-side list was reset.
        self.reregistered = False

    @property
    def _needs_refresh(self) -> bool:
        """Whether the cached token is missing, or inside the refresh margin."""
        if self._token is None or self._expires_at is None:
            return True
        return datetime.now(timezone.utc) >= self._expires_at - TOKEN_REFRESH_MARGIN

    def pop_reregistered(self) -> bool:
        """Return whether a recovery re-register happened, and clear the flag.

        Call once per poll. ``True`` means the carrier-side tracked-parcel
        list was reset and every tracked parcel must be re-``POST``ed.
        """
        value = self.reregistered
        self.reregistered = False
        return value

    async def async_register(self) -> str:
        """Mint a brand-new anonymous identity and return its ``appInstanceId``.

        Called once, at config-entry creation. Unconditional — always mints
        a fresh id even if one happens to already be set.
        """
        await self._async_register()
        assert self.app_instance_id is not None
        return self.app_instance_id

    async def async_get_token(self) -> str:
        """Return a valid bearer token for the parcel backend.

        Refreshes via ``validate`` when the cached token is missing or
        inside the refresh margin; recovers via a fresh ``register`` if the
        identity itself is gone server-side (a ``404`` from ``validate``).
        """
        if self.app_instance_id is None:
            raise GlsDeSessionError(
                "no appInstanceId to validate — call async_register first"
            )
        if self._needs_refresh:
            await self._async_refresh_or_recover()
        assert self._token is not None
        return self._token

    async def async_handle_unauthorized(self) -> str:
        """Force one refresh after a 401 from the parcel backend.

        Callers must retry the failing request exactly once with the
        returned token and then give up — never loop
        (BUILD_PLAN_DE.md §3.3).
        """
        if self.app_instance_id is None:
            raise GlsDeSessionError(
                "no appInstanceId to validate — call async_register first"
            )
        await self._async_refresh_or_recover()
        assert self._token is not None
        return self._token

    async def _async_refresh_or_recover(self) -> None:
        """``validate``, or re-``register`` if the instance is gone."""
        try:
            await self._async_validate()
        except _InstanceGoneError:
            await self._async_register(reregistering=True)

    async def _async_register(self, *, reregistering: bool = False) -> None:
        """``POST /users/register`` and store the fresh identity + token."""
        app_instance_id = str(uuid4())
        payload = await self._async_post_identity(
            REGISTER_URL, {"appInstanceId": app_instance_id}, expected_status=201
        )
        self.app_instance_id = app_instance_id
        self._store_token(payload)
        if reregistering:
            self.reregistered = True
            _LOGGER.warning(
                "GLS Germany registered a new anonymous app instance to "
                "recover from a gone one. Every tracked parcel will be "
                "re-added on the next poll."
            )

    async def _async_validate(self) -> None:
        """``POST /users/validate`` (no bearer) and store the refreshed token."""
        payload = await self._async_post_identity(
            VALIDATE_URL,
            {"appInstanceId": self.app_instance_id},
            expected_status=200,
            treat_404_as_gone=True,
        )
        self._store_token(payload)

    async def _async_post_identity(
        self,
        url: str,
        body: dict[str, Any],
        *,
        expected_status: int,
        treat_404_as_gone: bool = False,
    ) -> dict[str, Any]:
        """POST to the identity service with no ``Authorization`` header.

        Neither ``register`` nor ``validate`` ever sends a bearer — that is
        the one hardcoded exception in the app's auth interceptor and the
        whole way this identity bootstraps itself.
        """
        async with self._session.post(url, json=body) as response:
            text = await response.text()
            if _mentions_recaptcha(text, response.headers):
                _warn_recaptcha_once()
            if treat_404_as_gone and response.status == 404:
                _LOGGER.warning(
                    "GLS Germany's anonymous app instance is gone "
                    "server-side (validate → 404) — registering a new one. "
                    "The carrier-side tracked-parcel list (not your "
                    "configured list) was reset."
                )
                raise _InstanceGoneError
            if response.status != expected_status:
                raise GlsDeSessionError(
                    f"GLS DE identity service returned {response.status} "
                    f"for {url}",
                    status_code=response.status,
                )
            return json.loads(text) if text else {}

    def _store_token(self, payload: dict[str, Any]) -> None:
        """Cache the access token and its expiry from a register/validate body."""
        token = payload.get("accessToken")
        if not token:
            raise GlsDeSessionError(
                "GLS DE identity service response had no accessToken"
            )
        self._token = token
        issued_at = _parse_identity_timestamp(payload.get("issuedAt"))
        expires_at = _parse_identity_timestamp(payload.get("expiresAt"))
        self._expires_at = expires_at or (
            datetime.now(timezone.utc) + _EXPECTED_TOKEN_LIFETIME
        )
        if issued_at is not None and expires_at is not None:
            lifetime = expires_at - issued_at
            if abs(lifetime - _EXPECTED_TOKEN_LIFETIME) > _TOKEN_LIFETIME_TOLERANCE:
                _warn_token_lifetime_anomaly(lifetime)
