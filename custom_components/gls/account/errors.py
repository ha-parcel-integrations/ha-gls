"""Errors every account country raises, so the shared code never branches on one."""

from __future__ import annotations


class GlsAccountApiError(Exception):
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


class GlsAccountAuthError(GlsAccountApiError):
    """The account needs the user to sign in again."""


class GlsAccountFeedError(Exception):
    """A fetched feed is incomplete or not provably this account's own.

    Raised by a country's ``parse_feed`` before anything is published, so a
    partial or foreign list can never replace the previous snapshot.
    """
