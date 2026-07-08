from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import User
from ..security import current_user
from ..system_backup import build_export, restore_import

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/export")
def export_everything(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> Response:
    """Download a ZIP backup of everything for the current user (settings, keys,
    providers, personas, sessions, files, …). The ZIP contains decrypted secrets."""
    blob = build_export(db, user)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"multichat-backup-{stamp}.zip"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_everything(
    file: UploadFile,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """Restore a backup ZIP. This REPLACES all of the current user's existing data."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        summary = restore_import(db, user, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
    return {"ok": True, "imported": summary}
