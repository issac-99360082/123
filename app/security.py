"""Password hashing, API-key generation, and webhook signing."""

from __future__ import annotations

import hashlib
import hmac
import secrets

import bcrypt

API_KEY_PREFIX = "wp_live_"
_PREFIX_SAMPLE_LEN = 12  # stored in clear to make key listing/identification possible


def hash_password(password: str) -> str:
    # bcrypt caps at 72 bytes; pre-hash so long passphrases are not silently truncated.
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        digest = hashlib.sha256(password.encode("utf-8")).digest()
        return bcrypt.checkpw(digest, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, prefix, sha256_hash).

    The plaintext is returned to the caller exactly once and never stored.
    """
    raw = secrets.token_urlsafe(32)
    key = f"{API_KEY_PREFIX}{raw}"
    return key, key[:_PREFIX_SAMPLE_LEN], hash_api_key(key)


def hash_api_key(key: str) -> str:
    """Fast deterministic hash - API keys are high-entropy, so no KDF is needed
    and we need a constant-time indexable lookup."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_webhook_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(24)}"


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>", same scheme Stripe uses.

    Receivers verify with the shared secret so a payload cannot be forged.
    """
    signed_payload = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
