"""Symmetric encryption helpers for storing sensitive tokens in DB.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
The encryption key is derived from the application's JWT secret via SHA-256.
"""

import base64
import hashlib

from cryptography.fernet import Fernet


def _make_fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str, secret: str) -> str:
    """Encrypt a plaintext token; return base64-encoded ciphertext."""
    return _make_fernet(secret).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret: str) -> str:
    """Decrypt a previously encrypted token; return plaintext."""
    return _make_fernet(secret).decrypt(encrypted.encode()).decode()
