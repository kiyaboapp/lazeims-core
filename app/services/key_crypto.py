"""Encrypt/decrypt ExaMetrics API keys at rest using MultiFernet.

MultiFernet supports key rotation: the first key encrypts, all keys decrypt.
``PROCESSING_KEY_ENCRYPTION_KEYS`` is a comma-separated list of base64 Fernet
keys. When empty (existing deployments that have not yet set it), a key is
derived deterministically from ``session_secret_key`` so the upgrade is
transparent and no existing row is unreadable.

NEVER log or audit the plaintext key or the encryption keys.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, MultiFernet

from ..config import get_settings

_cached_fernet: MultiFernet | None = None


def _derive_key_from_secret(secret: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from an arbitrary secret string."""
    raw = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _get_fernet() -> MultiFernet:
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet

    settings = get_settings()
    raw_keys = getattr(settings, "processing_key_encryption_keys", "")
    keys_str = raw_keys.strip() if raw_keys else ""

    if keys_str:
        parts = [p.strip() for p in keys_str.split(",") if p.strip()]
        fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in parts]
    else:
        # Fallback: derive from session_secret_key so no deployment breaks.
        derived = _derive_key_from_secret(settings.session_secret_key)
        fernets = [Fernet(derived)]

    _cached_fernet = MultiFernet(fernets)
    return _cached_fernet


def reset_cache() -> None:
    """Clear the cached fernet instance (for testing)."""
    global _cached_fernet
    _cached_fernet = None


def encrypt_key(plaintext: str) -> str:
    """Encrypt a plaintext API key. Returns a base64 token string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_key(token: str) -> str:
    """Decrypt a stored encrypted API key token back to plaintext."""
    f = _get_fernet()
    return f.decrypt(token.encode()).decode()
