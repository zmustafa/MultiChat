from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import User
from ..schemas import AuthResponse, LoginRequest, UserOut
from ..security import (
    create_access_token,
    current_user,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: DbSession = Depends(get_db)) -> AuthResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    token = create_access_token(user.id)
    return AuthResponse(token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=dict)
def me(user: User = Depends(current_user)) -> dict:
    return {"user": UserOut.model_validate(user).model_dump(mode="json")}
