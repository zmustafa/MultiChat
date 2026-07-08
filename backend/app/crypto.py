from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _get_fernet() -> Fernet:
    key = settings.APP_ENCRYPTION_KEY.strip()
    if not key:
        # Derive a deterministic (dev-only) key so the app still boots without config.
        digest = hashlib.sha256(b"multichat-dev-fallback-key").digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


_fernet = _get_fernet()


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


def mask_secret(plaintext: str | None) -> str | None:
    """Return a masked representation of a secret, e.g. sk-...abcd."""
    if not plaintext:
        return None
    if len(plaintext) <= 8:
        return "•" * len(plaintext)
    return f"{plaintext[:3]}...{plaintext[-4:]}"
