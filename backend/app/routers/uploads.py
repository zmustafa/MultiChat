from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from ..db import get_db
from ..documents import extract_text, is_document, normalize_doc_mime
from ..models import Attachment, User
from ..schemas import AttachmentOut
from ..security import current_user

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _upload_dir() -> str:
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    return settings.UPLOAD_DIR


@router.post("", response_model=list[AttachmentOut])
async def upload(
    files: list[UploadFile],
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[AttachmentOut]:
    if len(files) > settings.MAX_UPLOADS_PER_TURN:
        raise HTTPException(
            status_code=400,
            detail=f"At most {settings.MAX_UPLOADS_PER_TURN} files per turn",
        )
    out: list[AttachmentOut] = []
    for f in files:
        is_image = f.content_type in settings.ALLOWED_IMAGE_TYPES
        doc = is_document(f.content_type or "", f.filename or "")
        if not is_image and not doc:
            raise HTTPException(
                status_code=400, detail=f"Unsupported file type: {f.content_type}"
            )
        data = await f.read()
        limit = settings.MAX_UPLOAD_BYTES if is_image else settings.MAX_DOC_BYTES
        if len(data) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"File exceeds {limit // (1024 * 1024)} MB limit",
            )
        att_id = str(uuid.uuid4())
        ext = os.path.splitext(f.filename or "")[1] or (".png" if is_image else ".bin")
        stored_name = f"{att_id}{ext}"
        path = os.path.join(_upload_dir(), stored_name)
        with open(path, "wb") as fh:
            fh.write(data)

        if is_image:
            kind = "image"
            mime = f.content_type
            extracted = None
        else:
            kind = "document"
            mime = normalize_doc_mime(f.content_type or "", f.filename or "")
            extracted = extract_text(data, mime, f.filename or "") or None

        att = Attachment(
            id=att_id,
            user_id=user.id,
            kind=kind,
            filename=f.filename or stored_name,
            mime_type=mime,
            size_bytes=len(data),
            storage_path=stored_name,
            extracted_text=extracted,
        )
        db.add(att)
        db.commit()
        out.append(
            AttachmentOut(
                id=att.id,
                filename=att.filename,
                mime_type=att.mime_type,
                size_bytes=att.size_bytes,
                kind=att.kind,
                url=f"/api/uploads/{att.id}",
            )
        )
    return out


@router.get("/{attachment_id}")
def get_upload(
    attachment_id: str,
    db: DbSession = Depends(get_db),
) -> FileResponse:
    # Served without an auth header so that plain <img src="…"> tags can load the image
    # (browsers can't attach a Bearer token to image requests). Attachment ids are
    # unguessable UUIDs; this app is single-user and local.
    att = db.get(Attachment, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = os.path.join(settings.UPLOAD_DIR, att.storage_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File missing")
    # Serve inline (no "attachment" Content-Disposition) so it renders in <img> tags.
    return FileResponse(
        path,
        media_type=att.mime_type,
        content_disposition_type="inline",
    )
