from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import User
from ..schemas import UserSettings
from ..security import current_user

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=UserSettings)
def get_settings(user: User = Depends(current_user)) -> UserSettings:
    return UserSettings(custom_instructions=user.custom_instructions)


@router.put("", response_model=UserSettings)
def update_settings(
    payload: UserSettings,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> UserSettings:
    user.custom_instructions = payload.custom_instructions
    db.add(user)
    db.commit()
    return UserSettings(custom_instructions=user.custom_instructions)
