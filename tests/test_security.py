"""Password hashing, API keys, webhook signatures, and the SSRF guard."""

from __future__ import annotations

import pytest

from app.core.fetcher import FetchError, sanitize_headers, validate_target_url
from app.security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    sign_webhook,
    verify_password,
)


def test_password_round_trip():
    hashed = hash_password("correct-horse-42")
    assert hashed != "correct-horse-42"
    assert verify_password("correct-horse-42", hashed)
    assert not verify_password("wrong-horse-42", hashed)


def test_long_passphrase_is_not_truncated_at_72_bytes():
    # Plain bcrypt ignores everything past 72 bytes; two long passphrases that
    # share a 72-byte prefix must not both validate.
    base = "a" * 72
    hashed = hash_password(base + "-tail-one")
    assert verify_password(base + "-tail-one", hashed)
    assert not verify_password(base + "-tail-two", hashed)


def test_verify_password_rejects_garbage_hash():
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_api_keys_are_unique_and_hashed():
    key_a, prefix_a, hash_a = generate_api_key()
    key_b, _, hash_b = generate_api_key()
    assert key_a != key_b and hash_a != hash_b
    assert key_a.startswith("wp_live_") and key_a.startswith(prefix_a)
    assert hash_a == hash_api_key(key_a)
    assert hash_a not in key_a  # the stored value never contains the secret


def test_webhook_signature_is_deterministic_and_binds_body_and_time():
    body = b'{"event":"monitor.changed"}'
    sig = sign_webhook("whsec_x", "1767225600", body)
    assert sig == sign_webhook("whsec_x", "1767225600", body)
    assert sig != sign_webhook("whsec_x", "1767225601", body)   # replay protection
    assert sig != sign_webhook("whsec_x", "1767225600", b"{}")  # tamper protection
    assert sig != sign_webhook("whsec_y", "1767225600", body)   # wrong secret


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
    ],
)
def test_ssrf_guard_blocks_unsafe_targets(url):
    with pytest.raises(FetchError):
        validate_target_url(url)


def test_public_url_is_allowed():
    assert validate_target_url("https://example.com/pricing").startswith("https://example.com")


def test_url_without_hostname_is_rejected():
    with pytest.raises(FetchError) as exc:
        validate_target_url("http:///nohost")
    assert exc.value.kind == "invalid_url"


def test_blocked_headers_are_stripped():
    clean = sanitize_headers(
        {"Host": "evil.example", "Connection": "close", "X-Api-Token": "keep-me"}
    )
    assert clean == {"X-Api-Token": "keep-me"}


def test_header_injection_is_dropped():
    assert sanitize_headers({"X-Bad": "value\r\nX-Injected: 1"}) == {}
