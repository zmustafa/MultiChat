from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..mcp.workiq import DEFAULT_ARGS, DEFAULT_COMMAND, EULA_URL, workiq
from ..models import Integration, User
from ..security import current_user

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class WorkIqConnectIn(BaseModel):
    command: str | None = None
    args: list[str] | None = None


class WorkIqTestIn(BaseModel):
    question: str = "What are my upcoming meetings this week?"


def _get_row(db: DbSession, user: User) -> Integration | None:
    return db.scalar(
        select(Integration).where(
            Integration.user_id == user.id, Integration.kind == "workiq"
        )
    )


@router.get("/workiq")
def workiq_status(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    row = _get_row(db, user)
    # Reflect the persisted EULA acceptance into the runtime manager.
    if row and row.eula_accepted:
        workiq.eula_accepted = True
    status = workiq.status()
    status["saved"] = bool(row and row.enabled)
    status["eula_accepted"] = bool((row and row.eula_accepted) or workiq.eula_accepted)
    status["eula_url"] = EULA_URL
    status["default_command"] = f"{DEFAULT_COMMAND} {' '.join(DEFAULT_ARGS)}"
    return status


@router.post("/workiq/accept-eula")
async def workiq_accept_eula(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    if not (workiq.enabled and workiq.connected):
        raise HTTPException(status_code=400, detail="Work IQ is not connected")
    tool = workiq.eula_tool_name()
    if not tool:
        raise HTTPException(status_code=400, detail="accept_eula tool not available")
    result = await workiq.call(tool, {"eulaUrl": EULA_URL})
    if result.strip().lower().startswith("[work iq error]") or result.strip().lower().startswith("[tool error]"):
        raise HTTPException(status_code=502, detail=result)
    workiq.eula_accepted = True
    row = _get_row(db, user)
    if row:
        row.eula_accepted = True
        db.commit()
    return {"result": result, "eula_url": EULA_URL, "eula_accepted": True}


@router.post("/workiq/connect")
async def workiq_connect(
    payload: WorkIqConnectIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    command = payload.command or DEFAULT_COMMAND
    args = payload.args if payload.args is not None else list(DEFAULT_ARGS)
    try:
        status = await workiq.connect(command, args)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc) or type(exc).__name__)
    # Persist so it can auto-reconnect on restart.
    row = _get_row(db, user)
    if row is None:
        row = Integration(user_id=user.id, kind="workiq")
        db.add(row)
    row.enabled = True
    row.command = command
    row.args_json = args
    db.commit()
    return status


@router.post("/workiq/test")
async def workiq_test(
    payload: WorkIqTestIn,
    user: User = Depends(current_user),  # noqa: ARG001
) -> dict:
    """Run a live test query against Work IQ (the `ask` tool) and return the answer."""
    if not (workiq.enabled and workiq.connected):
        raise HTTPException(status_code=400, detail="Work IQ is not connected")
    ask = next(
        (t["name"] for t in workiq.exposed_tools() if t["name"].endswith("_ask")),
        None,
    )
    if not ask:
        raise HTTPException(status_code=400, detail="Work IQ 'ask' tool not available")
    result = await workiq.call(ask, {"question": payload.question})
    ok = not (
        result.strip().lower().startswith("[work iq error]")
        or result.strip().lower().startswith("[tool error]")
    )
    return {"ok": ok, "question": payload.question, "result": result}


@router.post("/workiq/disconnect")
async def workiq_disconnect(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> dict:
    await workiq.disconnect()
    row = _get_row(db, user)
    if row:
        row.enabled = False
        db.commit()
    return workiq.status()
