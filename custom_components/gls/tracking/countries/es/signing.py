"""Request signing for the GLS Spain / Portugal consignee backend.

Kept separate from the transport, and pure, for one reason: a signature bug
and a rejected key look similar enough on the wire that the signature itself
has to be provable without a network. ``sign_request`` is therefore a
function of its three inputs and nothing else — no clock, no session, no
module state — so a frozen timestamp and a known key pin it exactly.

The one rule the transport must honour: **re-stamp and re-sign on every
attempt, including retries.** The service rejects a timestamp minutes from its
own clock, so a signature computed once and reused fails. Building the
signature above a retry loop is the bug this split exists to make obvious.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac


def request_timestamp(moment: datetime | None = None) -> str:
    """Return ``moment`` in the exact shape the service expects.

    ISO-8601 UTC with milliseconds and a literal ``Z`` — three fractional
    digits, not six: the service echoes the value back in its own rejection
    message, and this is the form it accepts. ``moment`` is injectable so a
    test can freeze it; production passes nothing.
    """
    moment = (moment or datetime.now(UTC)).astimezone(UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def sign_request(key: str, method: str, path: str, timestamp: str) -> str:
    """Return the lowercase hex HMAC-SHA256 for one request.

    The signed string is three newline-joined lines — the HTTP method, the
    path, and the same value sent in ``X-Timestamp``:

    .. code-block:: text

        POST
        /expeditions/find
        2026-01-01T00:00:00.000Z

    ``path`` is the route **without** the version prefix the URL carries, so
    ``/expeditions/find`` and never ``/api/v5/expeditions/find``. Signing the
    full path yields a well-formed signature the service rejects, which is the
    least obvious way to get this wrong.
    """
    message = f"{method}\n{path}\n{timestamp}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


__all__ = ["request_timestamp", "sign_request"]
