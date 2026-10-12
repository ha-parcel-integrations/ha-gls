"""Tests for the GLS Spain request signing.

The one piece of this build that can be proven correct without a network, and
the reason ``signing.py`` is pure: a signature bug and a rejected key are hard
to tell apart on the wire, so the signature itself is pinned here against a
frozen timestamp and a known key.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from custom_components.gls.countries.es.signing import request_timestamp, sign_request

KEY = "test-key"
TS = "2026-01-01T00:00:00.000Z"


def test_signature_is_hmac_sha256_over_three_newline_joined_lines():
    expected = hmac.new(
        KEY.encode(), f"POST\n/expeditions/find\n{TS}".encode(), hashlib.sha256
    ).hexdigest()
    assert sign_request(KEY, "POST", "/expeditions/find", TS) == expected


def test_signature_is_lowercase_hex():
    signature = sign_request(KEY, "POST", "/expeditions/find", TS)
    assert len(signature) == 64
    assert signature == signature.lower()
    assert all(char in "0123456789abcdef" for char in signature)


def test_signed_path_excludes_the_version_prefix():
    """Signing the URL's full path yields a well-formed signature the service
    rejects — the least obvious way to get this wrong, so it is pinned."""
    assert sign_request(KEY, "POST", "/expeditions/find", TS) != sign_request(
        KEY, "POST", "/api/v5/expeditions/find", TS
    )


def test_every_input_changes_the_signature():
    base = sign_request(KEY, "POST", "/expeditions/find", TS)
    assert sign_request("other-key", "POST", "/expeditions/find", TS) != base
    assert sign_request(KEY, "GET", "/expeditions/find", TS) != base
    assert sign_request(KEY, "POST", "/parcelShops/find", TS) != base
    assert sign_request(KEY, "POST", "/expeditions/find", "2026-01-01T00:00:01.000Z") != base


def test_timestamp_has_exactly_three_fractional_digits_and_a_literal_z():
    moment = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    assert request_timestamp(moment) == "2026-01-02T03:04:05.678Z"


def test_timestamp_truncates_rather_than_rounds_microseconds():
    moment = datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=timezone.utc)
    assert request_timestamp(moment) == "2026-01-02T03:04:05.999Z"


def test_timestamp_pads_sub_millisecond_values():
    moment = datetime(2026, 1, 2, 3, 4, 5, 7000, tzinfo=timezone.utc)
    assert request_timestamp(moment) == "2026-01-02T03:04:05.007Z"


def test_timestamp_converts_a_non_utc_moment_to_utc():
    """The service compares against its own clock, so the value must be UTC
    regardless of the zone the caller's datetime carries."""
    moment = datetime(
        2026, 1, 2, 5, 4, 5, 0, tzinfo=timezone(timedelta(hours=2))
    )
    assert request_timestamp(moment) == "2026-01-02T03:04:05.000Z"


def test_timestamp_defaults_to_now():
    assert request_timestamp().endswith("Z")
