"""Security primitives: Argon2id password/PIN hashing and opaque token minting.

Human passwords and station PINs are both hashed with Argon2id. Session ids and
CSRF tokens are opaque, high-entropy random strings.
"""

from __future__ import annotations

import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

_ph = PasswordHasher()  # Argon2id defaults are sound for this use.


def hash_secret(plaintext: str) -> str:
    """Hash a password or PIN with Argon2id."""
    return _ph.hash(plaintext)


def verify_secret(hashed: str, plaintext: str) -> bool:
    """Constant-time-ish verification. Returns False on any mismatch/format error."""
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _ph.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def new_token(nbytes: int = 32) -> str:
    """URL-safe opaque token (session id, CSRF token, station key)."""
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
