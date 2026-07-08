from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import AnswerSnapshot, User
from ..security import current_user

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class SnapshotCreate(BaseModel):
    session_id: str | None = None
    prompt: str = ""
    model: str
    provider_name: str | None = None
    content: str = ""
    label: str | None = None


class SnapshotOut(BaseModel):
    id: str
    session_id: str | None
    prompt: str
    model: str
    provider_name: str | None
    content: str
    label: str | None
    created_at: datetime


def _out(s: AnswerSnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=s.id,
        session_id=s.session_id,
        prompt=s.prompt,
        model=s.model,
        provider_name=s.provider_name,
        content=s.content,
        label=s.label,
        created_at=s.created_at,
    )


@router.get("", response_model=list[SnapshotOut])
def list_snapshots(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[SnapshotOut]:
    rows = db.scalars(
        select(AnswerSnapshot)
        .where(AnswerSnapshot.user_id == user.id)
        .order_by(AnswerSnapshot.created_at.desc())
    ).all()
    return [_out(s) for s in rows]


@router.post("", response_model=SnapshotOut)
def create_snapshot(
    payload: SnapshotCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SnapshotOut:
    s = AnswerSnapshot(
        user_id=user.id,
        session_id=payload.session_id,
        prompt=payload.prompt or "",
        model=payload.model,
        provider_name=payload.provider_name,
        content=payload.content or "",
        label=payload.label,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _out(s)


@router.delete("/{snapshot_id}")
def delete_snapshot(
    snapshot_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    s = db.get(AnswerSnapshot, snapshot_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    db.delete(s)
    db.commit()
    return {"ok": True}
