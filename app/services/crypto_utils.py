"""Symmetric encryption helpers for storing sensitive tokens in DB.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.

Tokens are encrypted with a dedicated key (``MVP_TOKEN_ENC_KEY``) that is kept
SEPARATE from the JWT secret. This matters because rotating ``MVP_JWT_SECRET``
is a standard security practice — if token encryption were tied to it, rotation
would irreversibly turn every stored T-Bank token into garbage (silent
``InvalidToken`` on every autosync).

Backward compatibility: legacy tokens were encrypted with a key derived from
``jwt_secret`` (``SHA256(jwt_secret)``). On decryption we try the active key
first, then fall back to the legacy key. Callers can re-encrypt with the active
key on a successful legacy read (lazy migration) via ``decrypt_and_maybe_migrate``.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _derive_key(secret: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from an arbitrary secret string."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _make_fernet(secret: str) -> Fernet:
    return Fernet(_derive_key(secret))


def _active_secret() -> str:
    """The secret currently used for *new* encryptions.

    Prefers the dedicated ``MVP_TOKEN_ENC_KEY``; falls back to ``jwt_secret``
    when it's unset (legacy / not-yet-configured deployments)."""
    return settings.token_enc_key or settings.jwt_secret


def encrypt_token(token: str, secret: str | None = None) -> str:
    """Encrypt a plaintext token; return base64-encoded ciphertext.

    ``secret`` is optional and primarily for tests; production callers should
    omit it so the active key (``MVP_TOKEN_ENC_KEY`` → jwt_secret fallback) is
    used automatically."""
    s = secret if secret is not None else _active_secret()
    return _make_fernet(s).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret: str | None = None) -> str:
    """Decrypt a previously encrypted token; return plaintext.

    With an explicit ``secret`` only that key is tried (used by tests). Without
    it, the active key is tried first, then the legacy jwt_secret-derived key,
    so tokens written before ``MVP_TOKEN_ENC_KEY`` existed still decrypt."""
    if secret is not None:
        return _make_fernet(secret).decrypt(encrypted.encode()).decode()
    plaintext, _ = decrypt_and_maybe_migrate(encrypted)
    return plaintext


def decrypt_and_maybe_migrate(encrypted: str) -> tuple[str, str | None]:
    """Decrypt using the active key, falling back to the legacy jwt_secret key.

    Returns ``(plaintext, new_ciphertext_or_None)``. ``new_ciphertext`` is set
    only when the token was decrypted with a *non-active* key and could be
    re-encrypted under the active one — the caller should persist it (lazy
    re-encrypt). When the active and legacy keys coincide (no dedicated key
    configured) no migration is signalled.

    Raises ``InvalidToken`` if no known key can decrypt the ciphertext."""
    active = _active_secret()
    raw = encrypted.encode()

    # 1) Try the active key first — the common case.
    try:
        plaintext = _make_fernet(active).decrypt(raw).decode()
        return plaintext, None
    except InvalidToken:
        pass

    # 2) Fall back to the legacy jwt_secret-derived key. If it equals the active
    #    key there's nothing else to try → re-raise the original failure.
    legacy = settings.jwt_secret
    if legacy == active:
        raise InvalidToken("token cannot be decrypted with the active key")

    plaintext = _make_fernet(legacy).decrypt(raw).decode()
    # Decrypted with legacy key → re-encrypt under the active key for migration.
    new_ciphertext = _make_fernet(active).encrypt(plaintext.encode()).decode()
    return plaintext, new_ciphertext
