from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import Snippet, User
from ..schemas import SnippetCreate, SnippetOut, SnippetUpdate
from ..security import current_user

router = APIRouter(prefix="/api/snippets", tags=["snippets"])


def _owned(db: DbSession, user: User, snippet_id: str) -> Snippet:
    s = db.get(Snippet, snippet_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="Snippet not found")
    return s


@router.get("", response_model=list[SnippetOut])
def list_snippets(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[SnippetOut]:
    rows = db.scalars(
        select(Snippet).where(Snippet.user_id == user.id).order_by(Snippet.title)
    ).all()
    return [SnippetOut.model_validate(s) for s in rows]


@router.post("", response_model=SnippetOut)
def create_snippet(
    payload: SnippetCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SnippetOut:
    s = Snippet(user_id=user.id, title=payload.title, content=payload.content)
    db.add(s)
    db.commit()
    db.refresh(s)
    return SnippetOut.model_validate(s)


@router.patch("/{snippet_id}", response_model=SnippetOut)
def update_snippet(
    snippet_id: str,
    payload: SnippetUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> SnippetOut:
    s = _owned(db, user, snippet_id)
    if payload.title is not None:
        s.title = payload.title
    if payload.content is not None:
        s.content = payload.content
    db.commit()
    db.refresh(s)
    return SnippetOut.model_validate(s)


@router.delete("/{snippet_id}", status_code=204)
def delete_snippet(
    snippet_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    s = _owned(db, user, snippet_id)
    db.delete(s)
    db.commit()
