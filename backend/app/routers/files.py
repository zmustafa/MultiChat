from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from ..db import get_db
from ..models import GeneratedFile, User
from ..schemas import GeneratedFileOut
from ..security import current_user
from ..tools.pptx_generate import GENERATED_SUBDIR

router = APIRouter(prefix="/api/files", tags=["files"])

# Files are stored as "<uuid hex>.<ext>"; only serve those exact, unguessable names.
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(pptx|docx|xlsx|pdf|md|txt)$")

_MIME_BY_EXT = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "txt": "text/plain",
}


def _safe_download_name(name: str | None, fallback: str) -> str:
    if not name:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip()
    return cleaned or fallback


@router.get("/{filename}")
def get_generated_file(
    filename: str,
    name: str | None = Query(default=None),
) -> FileResponse:
    """Serve a generated downloadable file (e.g. a .pptx deck) as an attachment.

    Served without an auth header so plain browser download links work (browsers can't
    attach a Bearer token). File names are unguessable UUID hex + a fixed extension, and
    are validated to prevent path traversal.
    """
    if not _NAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="File not found")
    path = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    ext = filename.rsplit(".", 1)[1]
    download_name = _safe_download_name(name, filename)
    return FileResponse(
        path,
        media_type=_MIME_BY_EXT.get(ext, "application/octet-stream"),
        filename=download_name,
        content_disposition_type="attachment",
    )


@router.get("/session/{session_id}", response_model=list[GeneratedFileOut])
def list_session_files(
    session_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[GeneratedFileOut]:
    rows = db.scalars(
        select(GeneratedFile)
        .where(
            GeneratedFile.user_id == user.id,
            GeneratedFile.session_id == session_id,
        )
        .order_by(GeneratedFile.created_at.desc())
    ).all()
    out: list[GeneratedFileOut] = []
    for r in rows:
        path = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR, r.stored_name)
        if not os.path.exists(path):
            continue
        out.append(
            GeneratedFileOut(
                id=r.id,
                stored_name=r.stored_name,
                download_name=r.download_name,
                mime_type=r.mime_type,
                size_bytes=r.size_bytes,
                kind=r.kind,
                url=f"/api/files/{r.stored_name}?name={r.download_name}",
                created_at=r.created_at,
            )
        )
    return out


@router.delete("/{filename}")
def delete_generated_file(
    filename: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    if not _NAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="File not found")
    row = db.scalar(
        select(GeneratedFile).where(
            GeneratedFile.stored_name == filename,
            GeneratedFile.user_id == user.id,
        )
    )
    path = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR, filename)
    if row is None and not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    if row is not None:
        db.delete(row)
        db.commit()
    return {"ok": True}
