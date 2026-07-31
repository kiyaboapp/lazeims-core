"""Tests for API key encryption at rest (C8 / D4).

Verifies:
- Round-trip encrypt/decrypt produces the original plaintext.
- The encrypted value is NOT the plaintext.
- A second key in the list still decrypts rows written under the first.
- The fallback derivation from session_secret_key works.
- The ExamProcessingLink property is transparent to callers.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.services import key_crypto
from app.services.key_crypto import decrypt_key, encrypt_key, reset_cache


@pytest.fixture(autouse=True)
def _reset_crypto_cache():
    """Ensure each test gets a fresh fernet instance."""
    reset_cache()
    yield
    reset_cache()


def test_round_trip_with_derived_key():
    """When PROCESSING_KEY_ENCRYPTION_KEYS is empty, derivation from session_secret_key works."""
    plaintext = "exmk_test1234_secretvalue"
    encrypted = encrypt_key(plaintext)
    assert encrypted != plaintext
    assert decrypt_key(encrypted) == plaintext


def test_encrypted_value_is_not_plaintext():
    plaintext = "exmk_visible_part_secret"
    encrypted = encrypt_key(plaintext)
    assert plaintext not in encrypted


def test_rotation_decrypts_with_old_key():
    """A second key added to the list still decrypts data encrypted by the first."""
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()

    # Encrypt under key1 only.
    with patch.dict(os.environ, {"PROCESSING_KEY_ENCRYPTION_KEYS": key1}):
        reset_cache()
        encrypted = encrypt_key("secret_data")

    # Now rotate: key2 is first (encrypts), key1 is second (still decrypts).
    with patch.dict(os.environ, {"PROCESSING_KEY_ENCRYPTION_KEYS": f"{key2},{key1}"}):
        reset_cache()
        result = decrypt_key(encrypted)
        assert result == "secret_data"


def test_new_encryptions_use_first_key():
    """New encryptions use the first key in the list."""
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()

    with patch.dict(os.environ, {"PROCESSING_KEY_ENCRYPTION_KEYS": f"{key1},{key2}"}):
        reset_cache()
        encrypted = encrypt_key("data")

    # Verify key1 alone can decrypt it (it is the primary).
    with patch.dict(os.environ, {"PROCESSING_KEY_ENCRYPTION_KEYS": key1}):
        reset_cache()
        assert decrypt_key(encrypted) == "data"


def test_model_property_transparent():
    """The ExamProcessingLink.api_key property encrypts/decrypts transparently."""
    import uuid
    from app.models.processing import ExamProcessingLink

    # Instantiate through normal constructor (SQLAlchemy init).
    link = ExamProcessingLink(
        exam_id=uuid.uuid4(),
        api_key_encrypted="",  # will be set via property
    )
    # Set via property
    link.api_key = "exmk_prefix_thesecretpart"
    # The stored column value should be encrypted, not plaintext
    assert link.api_key_encrypted != "exmk_prefix_thesecretpart"
    assert "exmk_prefix_thesecretpart" not in link.api_key_encrypted
    # Reading back via property should return plaintext
    assert link.api_key == "exmk_prefix_thesecretpart"


def test_model_property_roundtrip_no_db():
    """Property works without database, just in-memory."""
    import uuid
    from app.models.processing import ExamProcessingLink

    link = ExamProcessingLink(
        exam_id=uuid.uuid4(),
        api_key_encrypted="",
    )
    link.api_key = "test_key_value_123"
    assert link.api_key == "test_key_value_123"
    assert link.api_key_encrypted != "test_key_value_123"
