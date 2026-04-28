"""
Symmetric-encrypted model fields (Fernet / AES-128-CBC + HMAC).

Why a prefix-based format and not just "always encrypted":
  When we rolled encryption into existing Account rows that were stored as
  plaintext, we needed the field to still read them without crashing. Every
  encrypted value is tagged with `fernet:v1:` — anything without the tag is
  treated as legacy plaintext on read, and re-encrypted on the next save.

Key source:
  settings.DB_ENCRYPTION_KEY (Fernet-format). If unset, config/settings.py
  derives one from SECRET_KEY via PBKDF2 so the project still works in dev.
  For production, set DB_ENCRYPTION_KEY explicitly so SECRET_KEY rotation
  doesn't silently break decryption.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


_ENC_PREFIX = 'fernet:v1:'
_fernet_singleton = None


def _fernet():
    global _fernet_singleton
    if _fernet_singleton is None:
        key = getattr(settings, 'DB_ENCRYPTION_KEY', None)
        if not key:
            raise RuntimeError("settings.DB_ENCRYPTION_KEY is not configured")
        if isinstance(key, str):
            key = key.encode()
        _fernet_singleton = Fernet(key)
    return _fernet_singleton


def encrypt_str(plain):
    """Wrap a plaintext string as `fernet:v1:<token>`. Idempotent."""
    if plain is None:
        return None
    if not isinstance(plain, str):
        plain = str(plain)
    if plain == '':
        return ''
    if plain.startswith(_ENC_PREFIX):
        return plain  # already encrypted
    token = _fernet().encrypt(plain.encode()).decode()
    return _ENC_PREFIX + token


def decrypt_str(stored):
    """Return plaintext from a stored value. Legacy un-prefixed rows are
    returned as-is so they remain readable until next write."""
    if stored is None:
        return None
    if not isinstance(stored, str):
        return stored
    if not stored.startswith(_ENC_PREFIX):
        return stored  # legacy plaintext
    try:
        return _fernet().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        # Corrupted or encrypted with a different key. Don't crash the UI —
        # return the stored value so the operator notices via the admin.
        return stored


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


class EncryptedTextField(models.TextField):
    """TextField that transparently encrypts on write / decrypts on read."""

    description = "Fernet-encrypted TextField"

    def from_db_value(self, value, expression, connection):
        return decrypt_str(value)

    def to_python(self, value):
        # Called by forms / deserialization. If we see a stored (encrypted)
        # value, decrypt it; otherwise accept as-is.
        if value is None:
            return None
        return decrypt_str(value) if is_encrypted(value) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_str(value)
