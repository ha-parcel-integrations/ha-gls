"""One Polish myGLS account's sign-in and renewable session.

Unlike Denmark's Firebase login, Poland authenticates through Azure AD B2C: an
OAuth authorization-code + PKCE flow that the mobile app drives through a
hidden web view. We reproduce it headlessly — fetch the authorize page, read
its ``csrf``/``transId``, post the phone and password to ``SelfAsserted``,
follow ``confirmed`` to the authorization code, and exchange it at the token
endpoint. The refresh token lives 90 days and is all we persist.

The myGLS API itself needs no app key and no request signature (confirmed on
the wire): just the bearer token and a per-session ``sessionid``. The backend
keeps a server-side session, so a token is only accepted after the device
session is registered once — ``POST /user/session`` with a client-minted
``sessionId`` UUID (the backend validates it under the field name ``deviceId``,
but the JSON key is ``sessionId``).

The B2C browser flow is run on a throwaway, cookie-isolated client session:
its anti-forgery cookies carry a ``|`` in their names, which the shared Home
Assistant cookie jar drops, and the ``tx``/``csrf_token`` query values must be
sent with a literal ``=`` rather than ``%3D``. Both quirks stay contained
here; the API calls use the shared session with plain bearer headers.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Callable
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import uuid

import aiohttp
import yarl

from ....const import CONF_ACCOUNT_REFRESH_TOKEN, CONF_ACCOUNT_UID
from ...errors import GlsAccountApiError, GlsAccountAuthError

_LOGGER = logging.getLogger(__name__)

_B2C_HOST = "https://login.gls-group.net"
_TENANT = "glsgroup.onmicrosoft.com"
_POLICY = "B2C_1A_PL_LOCAL_PHONE_SI"
_CLIENT_ID = "79ba7504-1b19-4145-8251-09e4fca2ec31"
# MSAL registers the signature hash already percent-encoded, so the literal
# redirect URI contains %2B/%2F/%3D; normal encoding sends them as %252B etc.,
# which is exactly what the app does.
_REDIRECT_URI = "msauth://pl.gls.mygls/9H%2B2MXEWxpwphWNe%2Bd%2Fxmm9CpFw%3D"
_SCOPE = f"openid offline_access https://{_TENANT}/{_CLIENT_ID}/default"
_AUTHORIZE = f"{_B2C_HOST}/{_TENANT}/oauth2/v2.0/authorize"
_SELF_ASSERTED = f"{_B2C_HOST}/{_TENANT}/{_POLICY}/SelfAsserted"
_CONFIRMED = f"{_B2C_HOST}/{_TENANT}/{_POLICY}/api/CombinedSigninAndSignup/confirmed"
_TOKEN = f"{_B2C_HOST}/{_TENANT}/oauth2/v2.0/token?p={_POLICY.lower()}"

_API_BASE = "https://mygls.gls-poland.com.pl/api/v1"
_SESSION_URL = f"{_API_BASE}/mygls-user/user/session"
_FEED_URL = f"{_API_BASE}/mygls-tracking/tracking/shipment/paged"
_DETAILS_URL = f"{_API_BASE}/mygls-tracking/tracking/shipment/details"

# One client identity for every request, the B2C browser steps included: the
# authorize page answered identically (200, csrf/transId, cookies) to this, to
# Home Assistant's own and to no User-Agent at all, and a real sign-in plus
# feed fetch completed end to end with it (both 2026-11-20), so nothing
# justifies a browser User-Agent.
_APP_UA = "myGLS/2.2.0-253 (Android 15; Home Assistant) Flutter"

_MAX_BODY_BYTES = 4 * 1024 * 1024
_TIMEOUT = 20
# The session-state key, and the ``entry.data`` key, holding the sign-in phone.
LOGIN_FIELD = "phone"
_REFRESH_MARGIN = 60
# The key under which each feed row carries its fetched details payload.
DETAILS_KEY = "_gls_details"
_PAGE_SIZE = 50
_MAX_PAGES = 20
_REJECTED_STATUSES = (401, 403)
# ``SelfAsserted`` answers 200 with a JSON ``status``: "200" accepts the
# credentials and "400" is B2C's credential-validation refusal (unknown number,
# wrong password). Any other value — throttling, a server fault — is transient
# and must not send the user to reauth.
_SELF_ASSERTED_REFUSED = "400"
# The token endpoint's OAuth error codes that mean "sign in again"; any other
# refusal is retried later.
_AUTH_ERROR_CODES = frozenset(
    {
        "invalid_grant",
        "unauthorized_client",
        "interaction_required",
        "AADB2C90225",  # bad username/password
    }
)


class GlsPlApiError(GlsAccountApiError):
    """A transient Polish request or invalid response."""


class GlsPlAuthError(GlsPlApiError, GlsAccountAuthError):
    """The Polish account needs the user to sign in again."""


@contextlib.asynccontextmanager
async def _guarded(failure: str) -> AsyncIterator[None]:
    """Bound one request in time and turn transport faults into ``failure``."""
    try:
        async with asyncio.timeout(_TIMEOUT):
            yield
    except (aiohttp.ClientError, TimeoutError):
        raise GlsPlApiError(failure) from None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt_claim(token: str, claim: str) -> str | None:
    """Read one claim from a JWT without verifying it (identity only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error):
        return None
    value = data.get(claim)
    return value if isinstance(value, str) and value else None


class GlsPlSession:
    """Own one account's B2C tokens and server-side session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        uid: str | None = None,
        refresh_token: str | None = None,
        token_updated: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        """Restore refresh credentials while keeping access tokens process-local."""
        self._session = session
        self.uid = uid
        # Learned at sign-in only; a restored session leaves the stored phone
        # alone, because ``persisted_state`` never overwrites with an empty one.
        self.phone: str | None = None
        self.refresh_token = refresh_token
        self._token_updated = token_updated
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._session_id: str | None = None
        self._registered = False
        self._lock = asyncio.Lock()
        # Last good details per ``trackingUid``, so a transient details failure
        # keeps a parcel's weight and history instead of blanking them.
        self._details_cache: dict[str, dict] = {}

    def _state(self) -> dict[str, str]:
        """Return only what the owner must persist across restarts."""
        return {
            CONF_ACCOUNT_UID: self.uid or "",
            LOGIN_FIELD: self.phone or "",
            CONF_ACCOUNT_REFRESH_TOKEN: self.refresh_token or "",
        }

    # --- low-level request helpers -------------------------------------------

    async def _read_json(self, response: aiohttp.ClientResponse) -> Any:
        """Read a bounded JSON body, or None when it is not JSON."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(16384):
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                raise GlsPlApiError("GLS Poland response is too large")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            return None

    async def _api_request(self, method: str, url: str, **kwargs: Any) -> Any:
        """One authenticated API call, refreshing a rejected token once."""
        token = await self.async_get_token()
        try:
            return await self._raw_api(method, url, token, **kwargs)
        except GlsPlApiError as err:
            if err.status_code not in _REJECTED_STATUSES:
                raise
        # A 401/403 can mean an expired token or a dropped session; refresh and
        # re-register before the single retry.
        self._registered = False
        token = await self.async_get_token(rejected_token=token)
        return await self._raw_api(method, url, token, **kwargs)

    async def _raw_api(self, method: str, url: str, token: str, **kwargs: Any) -> Any:
        """Issue one API request with the bearer and session headers."""
        await self._ensure_session(token)
        headers = {**self._api_headers(token), **kwargs.pop("headers", {})}
        async with (
            _guarded("GLS Poland request failed"),
            self._session.request(
                method, url, headers=headers, allow_redirects=False, **kwargs
            ) as response,
        ):
            status = response.status
            body = await self._read_json(response)
        if status != 200:
            raise GlsPlApiError("GLS Poland request was rejected", status_code=status)
        return body

    # --- token lifecycle -----------------------------------------------------

    async def async_get_token(self, *, rejected_token: str | None = None) -> str:
        """Refresh the access token once per expiry or rejected token."""
        async with self._lock:
            cached = self._access_token
            if (
                cached
                and cached != rejected_token
                and self._expires_at - time.monotonic() > _REFRESH_MARGIN
            ):
                return cached
            if not self.refresh_token:
                raise GlsPlAuthError("GLS Poland sign-in is required")
            body = await self._b2c_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": _CLIENT_ID,
                    "scope": _SCOPE,
                    "redirect_uri": _REDIRECT_URI,
                }
            )
            return self._apply_tokens(body)

    def _apply_tokens(self, body: dict) -> str:
        """Validate a token response and store the rotated credentials."""
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise GlsPlApiError("GLS Poland returned no access token")
        uid = _jwt_claim(access, "sub")
        if uid is None:
            raise GlsPlApiError("GLS Poland token carried no identity")
        if self.uid is not None and uid != self.uid:
            raise GlsPlAuthError("GLS Poland session identity changed")
        try:
            lifetime = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        self.uid = uid
        self._access_token = access
        self._expires_at = time.monotonic() + max(60, min(lifetime, 7 * 86400))
        previous = self.refresh_token
        if isinstance(refresh, str) and refresh:
            self.refresh_token = refresh
        if self._token_updated is not None and self.refresh_token != previous:
            self._token_updated(self._state())
        return access

    async def _b2c_token(self, data: dict[str, str]) -> dict:
        """POST the token endpoint and sort auth failures from transient ones."""
        async with (
            _guarded("GLS Poland token request failed"),
            self._session.post(
                _TOKEN,
                data=data,
                headers={"User-Agent": _APP_UA, "Accept": "*/*"},
                allow_redirects=False,
            ) as response,
        ):
            status = response.status
            body = await self._read_json(response)
        if status == 200 and isinstance(body, dict):
            return body
        code = body.get("error") if isinstance(body, dict) else None
        if isinstance(code, str) and code in _AUTH_ERROR_CODES:
            raise GlsPlAuthError("GLS Poland credentials were rejected")
        raise GlsPlApiError("GLS Poland token request was rejected", status_code=status)

    # --- interactive sign-in (Azure AD B2C) ----------------------------------

    async def async_login(self, phone: str, password: str) -> dict[str, str]:
        """Sign in with phone and password; never retain the password."""
        async with self._lock:
            jar = aiohttp.DummyCookieJar()
            async with aiohttp.ClientSession(cookie_jar=jar) as browser:
                code, verifier = await self._b2c_authorize(browser, phone, password)
            try:
                body = await self._b2c_token(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": verifier,
                        "client_id": _CLIENT_ID,
                        "redirect_uri": _REDIRECT_URI,
                    }
                )
            except GlsPlAuthError:
                # SelfAsserted already accepted the credentials; a refused code
                # exchange (PKCE, a reused code) is a flow fault, not a password.
                raise GlsPlApiError("GLS Poland sign-in did not complete") from None
            # Keep the 9-digit dialling form SelfAsserted accepted, never the
            # token's "+48…" name claim: reauth sends it back to SelfAsserted.
            self.phone = phone
            self._apply_tokens(body)
            self._registered = False
            return self._state()

    async def _b2c_authorize(
        self, browser: aiohttp.ClientSession, phone: str, password: str
    ) -> tuple[str, str]:
        """Drive the B2C web flow to an authorization code and PKCE verifier."""
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        params = {
            "p": _POLICY,
            "client_id": _CLIENT_ID,
            "nonce": secrets.token_urlsafe(24),
            "scope": _SCOPE,
            "response_type": "code",
            "prompt": "login",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(24),
            "ui_locales": "pl",
            # Already percent-encoded on purpose; ``urlencode`` encodes it once
            # more, which is exactly what the app sends.
            "redirect_uri": _REDIRECT_URI,
        }
        cookies: dict[str, str] = {}
        html = await self._browser_request(
            browser,
            "GET",
            yarl.URL(f"{_AUTHORIZE}?{urlencode(params)}", encoded=True),
            cookies,
        )
        match = re.search(
            r"SETTINGS\s*=\s*(\{.*?\})\s*;?\s*(?:</script>|$)", html, re.DOTALL
        )
        if not match:
            match = re.search(r"SETTINGS\s*=\s*(\{.*\})", html, re.DOTALL)
        try:
            settings = json.loads(match.group(1)) if match else {}
            csrf = settings["csrf"]
            trans_id = settings["transId"]
        except (ValueError, KeyError, AttributeError):
            # Changed markup is our problem to fix, not the user's password.
            raise GlsPlApiError("GLS Poland sign-in page was not understood") from None

        status_body = await self._browser_request(
            browser,
            "POST",
            self._raw_url(_SELF_ASSERTED, {"tx": trans_id, "p": _POLICY}),
            cookies,
            data={
                "request_type": "RESPONSE",
                "phoneNumber": phone,
                "password": password,
            },
            headers={
                "X-CSRF-TOKEN": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": _B2C_HOST,
                "Referer": f"{_AUTHORIZE}?p={_POLICY}",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        try:
            verdict = json.loads(status_body)
        except ValueError:
            verdict = None
        status = verdict.get("status") if isinstance(verdict, dict) else None
        if status == _SELF_ASSERTED_REFUSED:
            raise GlsPlAuthError("GLS Poland rejected the credentials")
        if status != "200":
            raise GlsPlApiError("GLS Poland sign-in response was not understood")

        location = await self._browser_request(
            browser,
            "GET",
            self._raw_url(
                _CONFIRMED,
                {
                    "rememberMe": "false",
                    "csrf_token": csrf,
                    "tx": trans_id,
                    "p": _POLICY,
                },
            ),
            cookies,
            want_location=True,
        )
        code = parse_qs(urlsplit(location).query).get("code", [None])[0]
        if not code:
            # The credentials were already accepted above; this is a flow fault.
            raise GlsPlApiError("GLS Poland sign-in did not complete")
        return code, verifier

    @staticmethod
    def _raw_url(base: str, params: dict[str, str]) -> yarl.URL:
        """Keep B2C state values' ``=`` literal, as the app sends them."""
        query = "&".join(f"{k}={quote(v, safe='=')}" for k, v in params.items())
        return yarl.URL(f"{base}?{query}", encoded=True)

    @staticmethod
    def _collect_cookies(response: aiohttp.ClientResponse, jar: dict[str, str]) -> None:
        for hop in (*response.history, response):
            for header in hop.headers.getall("Set-Cookie", []):
                name, _, value = header.split(";", 1)[0].partition("=")
                if name.strip():
                    jar[name.strip()] = value.strip()

    async def _browser_request(
        self,
        browser: aiohttp.ClientSession,
        method: str,
        url: yarl.URL,
        cookies: dict[str, str],
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        want_location: bool = False,
    ) -> str:
        """One B2C web step, carrying the hand-kept cookies; body or Location."""
        sent = {"User-Agent": _APP_UA, **(headers or {})}
        if cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        send = browser.post if method == "POST" else browser.get
        async with (
            _guarded("GLS Poland sign-in request failed"),
            send(url, data=data, headers=sent, allow_redirects=False) as response,
        ):
            self._collect_cookies(response, cookies)
            if want_location:
                return response.headers.get("Location", "")
            return await response.text()

    # --- server-side session + parcel feed -----------------------------------

    def _api_headers(self, token: str) -> dict[str, str]:
        """The bearer and session headers every myGLS API call carries."""
        return {
            "Authorization": f"Bearer {token}",
            "sessionid": self._session_id or "",
            "User-Agent": _APP_UA,
            "Accept": "application/json",
        }

    async def _ensure_session(self, token: str) -> None:
        """Register the device session once so the API accepts the token."""
        if self._registered:
            return
        if self._session_id is None:
            self._session_id = str(uuid.UUID(bytes=secrets.token_bytes(16)))
        payload = {
            "deviceName": "Home Assistant",
            "deviceType": "ANDROID",
            "sessionId": self._session_id,
        }
        headers = {**self._api_headers(token), "Content-Type": "application/json"}
        async with (
            _guarded("GLS Poland session registration failed"),
            self._session.post(
                _SESSION_URL, json=payload, headers=headers, allow_redirects=False
            ) as response,
        ):
            status = response.status
        if status not in (200, 201, 204):
            raise GlsPlApiError(
                "GLS Poland session registration was rejected", status_code=status
            )
        self._registered = True

    async def async_get_parcels(self) -> dict:
        """Fetch the incoming feed, enriching each parcel with its details."""
        items: list[dict] = []
        total: int | None = None
        for page in range(_MAX_PAGES):
            body = await self._api_request(
                "GET",
                _FEED_URL,
                params={"page": page, "size": _PAGE_SIZE, "archive": "false"},
            )
            if not isinstance(body, dict):
                raise GlsPlApiError("GLS Poland returned an invalid parcel list")
            content = body.get("content")
            if not isinstance(content, list):
                raise GlsPlApiError("GLS Poland returned an invalid parcel page")
            items.extend(item for item in content if isinstance(item, dict))
            total = body.get("totalElements")
            if body.get("last") is True or not content:
                break
        tracked: set[str] = set()
        for item in items:
            uid = item.get("trackingUid")
            if isinstance(uid, str) and uid:
                tracked.add(uid)
                item[DETAILS_KEY] = await self._fetch_details(uid)
        # Forget parcels that left the feed, so the cache stays bounded.
        for uid in self._details_cache.keys() - tracked:
            del self._details_cache[uid]
        return {"items": items, "totalElements": total}

    async def _fetch_details(self, tracking_uid: str) -> dict | None:
        """Fetch one parcel's details; a transient failure reuses the last good.

        A failed sign-in is not transient: it propagates so the account goes to
        reauth rather than quietly losing every parcel's details. Nor is a
        rejection that survives the forced refresh: it fails the whole poll.
        """
        try:
            body = await self._api_request(
                "GET",
                f"{_DETAILS_URL}/{quote(tracking_uid, safe='')}",
                params={"archive": "false"},
            )
        except GlsPlAuthError:
            raise
        except GlsPlApiError as err:
            # A 401/403 that survived ``_api_request``'s one forced refresh is
            # the backend refusing the session, not a blip: fail the poll (an
            # ordinary UpdateFailed) rather than refresh again for every parcel.
            if err.status_code in _REJECTED_STATUSES:
                raise
            return self._details_cache.get(tracking_uid)
        if not isinstance(body, dict):
            return self._details_cache.get(tracking_uid)
        self._details_cache[tracking_uid] = body
        return body
