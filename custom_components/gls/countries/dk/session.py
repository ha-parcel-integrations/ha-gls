"""One Danish GLS account's asynchronous sign-in and renewable session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import time
from typing import Any
from urllib.parse import quote

import aiohttp

DEFAULT_API_KEY = "AIzaSyDLiC4OY7FPufiaVk1cwMYkUHcV8O1cH_I"
_IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
_BACKEND_URL = "https://glsdk-app-gls-app-prod.azurewebsites.net"
_MAX_BODY_BYTES = 2 * 1024 * 1024
_REFRESH_MARGIN = 60
_REJECTED_STATUSES = (401, 403)
_AUTH_ERRORS = frozenset(
    {
        "INVALID_LOGIN_CREDENTIALS",
        "INVALID_PASSWORD",
        "EMAIL_NOT_FOUND",
        "INVALID_EMAIL",
        "USER_DISABLED",
        "INVALID_REFRESH_TOKEN",
        "TOKEN_EXPIRED",
        "USER_NOT_FOUND",
        "CREDENTIAL_TOO_OLD_LOGIN_AGAIN",
    }
)


class GlsDkApiError(Exception):
    """A transient request or invalid response; messages contain no remote values."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        """Keep safe status metadata for the coordinator's backoff."""
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class GlsDkAuthError(GlsDkApiError):
    """The account needs the user to sign in again."""


def _required_string(data: dict, key: str) -> str:
    """Read a nonempty, bounded string without echoing invalid values."""
    value = data.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 20000
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise GlsDkApiError("GLS Denmark returned an invalid response field")
    return value


def _retry_after(value: str | None) -> int | None:
    """Accept bounded delta-seconds; HTTP dates use the coordinator fallback."""
    try:
        seconds = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return min(seconds, 86400) if seconds > 0 else None


class GlsDkSession:
    """Own one account's ID token; persist only its renewable credentials."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str = DEFAULT_API_KEY,
        uid: str | None = None,
        refresh_token: str | None = None,
        token_updated: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        """Restore refresh credentials while keeping ID tokens process-local."""
        self._session = session
        self.api_key = api_key
        self.uid = uid
        self.email: str | None = None
        self.refresh_token = refresh_token
        self._token_updated = token_updated
        self._id_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def _state(self) -> dict[str, str]:
        """Return only the state the owner must persist across restarts."""
        return {
            "uid": self.uid or "",
            "email": self.email or "",
            "refresh_token": self.refresh_token or "",
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        firebase: bool = False,
        **kwargs: Any,
    ) -> dict:
        """Read a bounded JSON object from a fixed endpoint without redirects."""
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if firebase:
            kwargs["params"] = {"key": self.api_key}
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    allow_redirects=False,
                    **kwargs,
                ) as response:
                    status = response.status
                    delay = _retry_after(response.headers.get("Retry-After"))
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(16384):
                        size += len(chunk)
                        if size > _MAX_BODY_BYTES:
                            raise GlsDkApiError("GLS Denmark response is too large")
                        chunks.append(chunk)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise GlsDkApiError("GLS Denmark request failed") from None
        try:
            body = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            body = None
        if status != 200:
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("message") if isinstance(error, dict) else None
            if (
                firebase
                and isinstance(code, str)
                and code.split(" : ")[0] in _AUTH_ERRORS
            ):
                raise GlsDkAuthError(
                    "GLS Denmark credentials were rejected", status_code=status
                )
            raise GlsDkApiError(
                "GLS Denmark request was rejected",
                status_code=status,
                retry_after=delay,
            )
        if not isinstance(body, dict):
            raise GlsDkApiError("GLS Denmark returned invalid JSON")
        return body

    def _apply_tokens(self, body: dict, *, refreshed: bool) -> str:
        """Validate a complete token response before replacing any state."""
        uid_key, token_key, refresh_key, expiry_key = (
            ("user_id", "id_token", "refresh_token", "expires_in")
            if refreshed
            else ("localId", "idToken", "refreshToken", "expiresIn")
        )
        uid = _required_string(body, uid_key)
        token = _required_string(body, token_key)
        refresh = _required_string(body, refresh_key)
        try:
            expiry = int(body[expiry_key])
        except (KeyError, ValueError, TypeError, OverflowError):
            raise GlsDkApiError(
                "GLS Denmark returned an invalid token lifetime"
            ) from None
        if isinstance(body[expiry_key], bool) or not 0 < expiry <= 7 * 86400:
            raise GlsDkApiError("GLS Denmark returned an invalid token lifetime")
        if refreshed and (
            uid != self.uid or body.get("token_type", "Bearer") != "Bearer"
        ):
            raise GlsDkAuthError("GLS Denmark session identity changed")
        self.uid = uid
        self._id_token = token
        self.refresh_token = refresh
        self._expires_at = time.monotonic() + expiry
        return token

    async def async_login(self, email: str, password: str) -> dict[str, str]:
        """Sign in an existing account and verify the matching GLS profile."""
        async with self._lock:
            body = await self._request(
                "POST",
                _IDENTITY_URL,
                firebase=True,
                json={"email": email, "password": password, "returnSecureToken": True},
            )
            self._apply_tokens(body, refreshed=False)
            profile = await self._request(
                "GET",
                f"{_BACKEND_URL}/user/get/{quote(self.uid or '', safe='')}",
                token=self._id_token,
            )
            if profile.get("identityId") != self.uid:
                raise GlsDkAuthError("GLS Denmark profile identity changed")
            self.email = _required_string(profile, "email")
            state = self._state()
            if self._token_updated is not None:
                self._token_updated(state)
            return state

    async def async_get_token(self, *, rejected_token: str | None = None) -> str:
        """Refresh once per expiry or rejected token, including concurrent callers."""
        async with self._lock:
            cached = self._id_token
            if (
                cached
                and cached != rejected_token
                and self._expires_at - time.monotonic() > _REFRESH_MARGIN
            ):
                return cached
            if not self.uid or not self.refresh_token:
                raise GlsDkAuthError("GLS Denmark sign-in is required")
            previous_refresh = self.refresh_token
            body = await self._request(
                "POST",
                _REFRESH_URL,
                firebase=True,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            token = self._apply_tokens(body, refreshed=True)
            if (
                self._token_updated is not None
                and self.refresh_token != previous_refresh
            ):
                self._token_updated(self._state())
            return token

    async def async_get_parcels(self) -> dict:
        """Fetch this account's feed, refreshing a rejected bearer token once.

        A 401 or 403 from the backend does not prove the credentials are bad:
        only Firebase can say so, by refusing the forced refresh below. A
        rejection that survives it stays an ordinary, retryable API error.
        """
        token = await self.async_get_token()
        url = f"{_BACKEND_URL}/trackandtrace/v1/tome"
        params = {"userid": self.uid, "languagecode": "da"}
        try:
            return await self._request("GET", url, token=token, params=params)
        except GlsDkApiError as err:
            if err.status_code not in _REJECTED_STATUSES:
                raise
        token = await self.async_get_token(rejected_token=token)
        return await self._request("GET", url, token=token, params=params)
