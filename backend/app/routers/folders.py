from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import Folder, Session as ChatSession, User
from ..schemas import FolderCreate, FolderOut, FolderUpdate
from ..security import current_user

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _owned(db: DbSession, user: User, folder_id: str) -> Folder:
    f = db.get(Folder, folder_id)
    if not f or f.user_id != user.id:
        raise HTTPException(status_code=404, detail="Folder not found")
    return f


@router.get("", response_model=list[FolderOut])
def list_folders(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[FolderOut]:
    rows = db.scalars(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    ).all()
    return [FolderOut.model_validate(f) for f in rows]


@router.post("", response_model=FolderOut)
def create_folder(
    payload: FolderCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> FolderOut:
    f = Folder(user_id=user.id, name=payload.name)
    db.add(f)
    db.commit()
    db.refresh(f)
    return FolderOut.model_validate(f)


@router.patch("/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: str,
    payload: FolderUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> FolderOut:
    f = _owned(db, user, folder_id)
    if payload.name is not None:
        f.name = payload.name
    if payload.position is not None:
        f.position = payload.position
    db.commit()
    db.refresh(f)
    return FolderOut.model_validate(f)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(
    folder_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    f = _owned(db, user, folder_id)
    # detach sessions from the folder rather than deleting them
    for s in db.scalars(
        select(ChatSession).where(
            ChatSession.user_id == user.id, ChatSession.folder_id == folder_id
        )
    ):
        s.folder_id = None
    db.delete(f)
    db.commit()
