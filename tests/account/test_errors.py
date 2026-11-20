"""The shared account errors carry only safe metadata."""

from __future__ import annotations

from custom_components.gls.account.errors import (
    GlsAccountApiError,
    GlsAccountAuthError,
    GlsAccountFeedError,
)


def test_api_error_keeps_status_metadata():
    error = GlsAccountApiError("failed", status_code=429, retry_after=30)
    assert str(error) == "failed"
    assert (error.status_code, error.retry_after) == (429, 30)
    assert GlsAccountApiError("x").status_code is None


def test_auth_error_is_an_api_error_and_feed_error_is_neither():
    assert issubclass(GlsAccountAuthError, GlsAccountApiError)
    assert not issubclass(GlsAccountFeedError, GlsAccountApiError)
