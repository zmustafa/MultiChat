from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import User
from ..schemas import AuthResponse, ChangePasswordRequest, LoginRequest, UserOut
from ..security import (
    create_access_token,
    current_user,
    hash_password,
    is_default_credentials,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    out = UserOut.model_validate(user)
    out.is_default_password = is_default_credentials(user)
    return out


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: DbSession = Depends(get_db)) -> AuthResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    token = create_access_token(user.id)
    return AuthResponse(token=token, user=_user_out(user))


@router.get("/me", response_model=dict)
def me(user: User = Depends(current_user)) -> dict:
    return {"user": _user_out(user).model_dump(mode="json")}


@router.post("/change-password", response_model=dict)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new_password = (payload.new_password or "").strip()
    if len(new_password) < 6:
        raise HTTPException(
            status_code=400, detail="New password must be at least 6 characters"
        )
    if verify_password(new_password, user.password_hash):
        raise HTTPException(
            status_code=400, detail="New password must be different from the current one"
        )
    user.password_hash = hash_password(new_password)
    db.add(user)
    db.commit()
    return {"ok": True}

