"""Unit tests for app.services.crypto_utils (Fernet token encryption)."""

import pytest
from cryptography.fernet import InvalidToken

from app.config import settings
from app.services import crypto_utils
from app.services.crypto_utils import (
    encrypt_token,
    decrypt_token,
    decrypt_and_maybe_migrate,
)


SECRET = "test-jwt-secret"


# ── happy path (explicit secret — used by callers/tests) ──────────────────────

def test_round_trip():
    plaintext = "t.bank-api-token-12345"
    encrypted = encrypt_token(plaintext, SECRET)
    assert encrypted != plaintext  # actually encrypted
    assert decrypt_token(encrypted, SECRET) == plaintext


def test_ciphertext_differs_each_call():
    """Fernet embeds a random IV + timestamp → same input encrypts differently,
    but both decrypt back to the same plaintext."""
    a = encrypt_token("same", SECRET)
    b = encrypt_token("same", SECRET)
    assert a != b
    assert decrypt_token(a, SECRET) == decrypt_token(b, SECRET) == "same"


def test_unicode_round_trip():
    plaintext = "токен-с-кириллицей-🔐"
    assert decrypt_token(encrypt_token(plaintext, SECRET), SECRET) == plaintext


# ── edge cases ───────────────────────────────────────────────────────────────

def test_wrong_secret_fails():
    """A token encrypted with one secret can't be decrypted with another."""
    encrypted = encrypt_token("secret-data", SECRET)
    with pytest.raises(InvalidToken):
        decrypt_token(encrypted, "different-secret")


def test_corrupted_ciphertext_fails():
    encrypted = encrypt_token("data", SECRET)
    corrupted = encrypted[:-4] + "XXXX"
    with pytest.raises(InvalidToken):
        decrypt_token(corrupted, SECRET)


# ── dedicated key + legacy fallback ──────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch):
    """Set distinct jwt_secret and token_enc_key for migration scenarios."""
    monkeypatch.setattr(settings, "jwt_secret", "legacy-jwt-secret")
    monkeypatch.setattr(settings, "token_enc_key", "dedicated-token-key")
    return settings


def test_active_key_round_trip(keys):
    """With a dedicated key set, encrypt/decrypt (no explicit secret) round-trips
    using that key and signals no migration."""
    enc = encrypt_token("tbank-token")
    plaintext, migrated = decrypt_and_maybe_migrate(enc)
    assert plaintext == "tbank-token"
    assert migrated is None  # already under the active key
    assert decrypt_token(enc) == "tbank-token"


def test_legacy_token_read_and_migrated(keys):
    """A token encrypted under the legacy jwt_secret-derived key still decrypts,
    and a re-encrypted ciphertext under the active key is returned for migration."""
    # Simulate a token stored before MVP_TOKEN_ENC_KEY existed.
    legacy_enc = encrypt_token("legacy-token", settings.jwt_secret)

    plaintext, migrated = decrypt_and_maybe_migrate(legacy_enc)
    assert plaintext == "legacy-token"
    assert migrated is not None
    assert migrated != legacy_enc

    # The migrated ciphertext decrypts under the active key with no further migration.
    again, again_migrated = decrypt_and_maybe_migrate(migrated)
    assert again == "legacy-token"
    assert again_migrated is None


def test_rotating_jwt_secret_does_not_break_tokens(keys):
    """The whole point: rotating jwt_secret must NOT break stored tokens, because
    they're encrypted with the dedicated key."""
    enc = encrypt_token("survives-rotation")
    # Rotate the JWT secret.
    keys.jwt_secret = "brand-new-jwt-secret"
    assert decrypt_token(enc) == "survives-rotation"


def test_fallback_to_jwt_secret_when_no_dedicated_key(monkeypatch):
    """Without MVP_TOKEN_ENC_KEY, the active key falls back to jwt_secret
    (legacy behaviour) and no migration is ever signalled."""
    monkeypatch.setattr(settings, "jwt_secret", "only-jwt-secret")
    monkeypatch.setattr(settings, "token_enc_key", "")

    enc = encrypt_token("data")
    plaintext, migrated = decrypt_and_maybe_migrate(enc)
    assert plaintext == "data"
    assert migrated is None  # active == legacy → nothing to migrate


def test_undecryptable_token_raises(keys):
    """A token that matches neither active nor legacy key raises InvalidToken."""
    foreign = encrypt_token("x", "some-unrelated-secret")
    with pytest.raises(InvalidToken):
        decrypt_and_maybe_migrate(foreign)
