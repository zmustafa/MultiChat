from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..crypto import decrypt, encrypt, mask_secret
from ..db import get_db
from ..models import ToolCredential, User
from ..schemas import (
    TestResult,
    ToolCredentialOut,
    ToolCredentialUpdate,
    ToolDefOut,
)
from ..security import current_user
from ..tools.registry import all_tools

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[ToolDefOut])
def list_tools(user: User = Depends(current_user)) -> list[ToolDefOut]:
    return [
        ToolDefOut(
            name=t.definition.name,
            description=t.definition.description,
            parameters=t.definition.parameters,
        )
        for t in all_tools().values()
    ]


def _get_cred(db: DbSession, user: User, tool: str) -> ToolCredential | None:
    return db.scalar(
        select(ToolCredential).where(
            ToolCredential.user_id == user.id, ToolCredential.tool == tool
        )
    )


@router.get("/credentials", response_model=list[ToolCredentialOut])
def get_credentials(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[ToolCredentialOut]:
    rows = db.scalars(
        select(ToolCredential).where(ToolCredential.user_id == user.id)
    ).all()
    return [
        ToolCredentialOut(
            tool=c.tool,
            masked_key=mask_secret(decrypt(c.api_key_encrypted)),
            has_key=bool(c.api_key_encrypted),
            extra=dict(c.extra_json or {}),
        )
        for c in rows
    ]


@router.put("/credentials", response_model=ToolCredentialOut)
def set_credentials(
    payload: ToolCredentialUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ToolCredentialOut:
    cred = _get_cred(db, user, payload.tool)
    if not cred:
        cred = ToolCredential(user_id=user.id, tool=payload.tool)
        db.add(cred)
    if payload.api_key:
        cred.api_key_encrypted = encrypt(payload.api_key)
    if payload.extra is not None:
        cred.extra_json = {**(cred.extra_json or {}), **payload.extra}
    db.commit()
    db.refresh(cred)
    return ToolCredentialOut(
        tool=cred.tool,
        masked_key=mask_secret(decrypt(cred.api_key_encrypted)),
        has_key=bool(cred.api_key_encrypted),
        extra=dict(cred.extra_json or {}),
    )


@router.post("/search/test", response_model=TestResult)
async def test_search(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> TestResult:
    from ..tools.search_engines import normalize_engine, run_search

    cred = _get_cred(db, user, "web_search")
    key = decrypt(cred.api_key_encrypted) if cred else None
    engine = normalize_engine(cred.extra_json.get("engine") if cred and cred.extra_json else None)
    if not engine:
        engine = "brave" if key else "duckduckgo"
    if engine == "brave" and not key:
        return TestResult(ok=False, detail="No Brave Search key configured")
    try:
        results = await run_search(engine, "test", 1, key)
        label = "DuckDuckGo" if engine == "duckduckgo" else "Brave Search"
        if results:
            return TestResult(ok=True, detail=f"{label} OK — {len(results)} result(s)")
        return TestResult(ok=False, detail=f"{label} returned no results")
    except Exception as exc:  # noqa: BLE001
        return TestResult(ok=False, detail=str(exc))
