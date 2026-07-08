from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .db import get_db
from .models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)

# Known weak/placeholder values that must never be used to sign real tokens — otherwise
# anyone who reads the (public) repo could forge valid JWTs.
_WEAK_SECRETS = {"", "change-me", "changeme", "dev-secret", "secret", "please-change-me"}


def _resolve_jwt_secret() -> str:
    """Return the effective JWT signing secret.

    If a strong ``JWT_SECRET`` is configured, use it. Otherwise generate a random secret
    and persist it (so tokens survive restarts) instead of silently signing with a public
    default. Set a real ``JWT_SECRET`` for production / multi-instance deployments.
    """
    configured = (settings.JWT_SECRET or "").strip()
    if configured and configured not in _WEAK_SECRETS:
        return configured

    path = os.path.join(settings.UPLOAD_DIR, ".jwt_secret")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(generated)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logging.getLogger("uvicorn.error").warning(
            "JWT_SECRET is unset or uses a known weak default; generated a random secret "
            "at %s. Set a strong JWT_SECRET env var for production / multi-instance.",
            path,
        )
        return generated
    except OSError:
        # Last resort: an ephemeral per-process secret (tokens won't survive a restart,
        # but they are never forgeable with a public default).
        return secrets.token_urlsafe(48)


# Resolved once at import; strong per-install secret rather than a public default.
JWT_SECRET = _resolve_jwt_secret()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def current_user(
    token: str | None = Depends(oauth2_scheme),
    db: DbSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc
    try:
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        user_id = payload.get("sub")
        if not user_id:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    user = db.get(User, user_id)
    if not user:
        raise credentials_exc
    return user
