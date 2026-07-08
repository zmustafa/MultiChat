from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..db import get_db
from ..models import Persona, Provider, User
from ..providers.registry import build_provider
from ..schemas import (
    PersonaCreate,
    PersonaEnhanceIn,
    PersonaEnhanceOut,
    PersonaLane,
    PersonaOut,
    PersonaUpdate,
)
from ..security import current_user

router = APIRouter(prefix="/api/personas", tags=["personas"])


def _serialize(p: Persona) -> PersonaOut:
    return PersonaOut(
        id=p.id,
        name=p.name,
        description=p.description,
        system_prompt=p.system_prompt,
        tools_enabled=bool(p.tools_enabled),
        is_default=bool(p.is_default),
        lanes=[PersonaLane(**lane) for lane in (p.lanes_json or [])],
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _get_owned(db: DbSession, user: User, persona_id: str) -> Persona:
    p = db.get(Persona, persona_id)
    if not p or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Persona not found")
    return p


@router.get("", response_model=list[PersonaOut])
def list_personas(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[PersonaOut]:
    rows = db.scalars(
        select(Persona)
        .where(Persona.user_id == user.id)
        .order_by(Persona.name)
    ).all()
    return [_serialize(p) for p in rows]


@router.post("", response_model=PersonaOut)
def create_persona(
    payload: PersonaCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> PersonaOut:
    p = Persona(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        tools_enabled=payload.tools_enabled,
        lanes_json=[lane.model_dump() for lane in payload.lanes],
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.post("/enhance", response_model=PersonaEnhanceOut)
async def enhance_persona(
    payload: PersonaEnhanceIn,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> PersonaEnhanceOut:
    """Use the user's default (or a chosen) provider/model to generate or improve a
    persona system prompt."""
    prov: Provider | None = None
    if payload.provider_id:
        prov = db.get(Provider, payload.provider_id)
        if not prov or prov.user_id != user.id:
            raise HTTPException(status_code=404, detail="Provider not found")
    if prov is None:
        provs = db.scalars(
            select(Provider).where(Provider.user_id == user.id)
        ).all()

        def _usable(p: Provider) -> bool:
            return bool(p.default_model or (p.models_json or []))

        # Prefer the user's default provider if it has a usable model, then any
        # provider with a model, then anything at all.
        prov = (
            next((p for p in provs if p.is_default and _usable(p)), None)
            or next((p for p in provs if _usable(p)), None)
            or (provs[0] if provs else None)
        )
    if prov is None:
        raise HTTPException(status_code=400, detail="No AI provider configured")

    model = payload.model or prov.default_model or (
        (prov.models_json or [None])[0] if prov.models_json else ""
    )
    if not model:
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{prov.name}' has no default model set.",
        )

    system = (
        "You are an expert prompt engineer who writes clear, effective SYSTEM PROMPTS "
        "that define an AI assistant's persona, expertise, tone, constraints and "
        "behavior. Return ONLY the system prompt text — no preamble, no explanation, "
        "no surrounding markdown code fences."
    )
    if payload.mode == "generate":
        task = "Write a high-quality, production-ready system prompt for the AI persona described below."
    else:
        task = (
            "Improve and expand the following AI persona system prompt while preserving "
            "its original intent. Make it more specific, well-structured, and effective."
        )
    parts = [task]
    if payload.name:
        parts.append(f"Persona name: {payload.name}")
    if payload.description:
        parts.append(f"Description: {payload.description}")
    if payload.system_prompt:
        parts.append(f"Current system prompt:\n{payload.system_prompt}")
    if payload.instruction:
        parts.append(f"Additional guidance from the user: {payload.instruction}")
    parts.append("Return ONLY the resulting system prompt.")
    user_msg = "\n\n".join(parts)

    llm = await build_provider(prov, db, model)
    text = ""
    try:
        async for ev in llm.stream(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            None,
        ):
            if ev.type == "token" and ev.text:
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Enhancement failed: {exc}")

    text = text.strip()
    # Strip accidental surrounding markdown code fences.
    if text.startswith("```"):
        text = text[3:]
        nl = text.find("\n")
        if nl != -1:
            first = text[:nl].strip()
            if not first or " " not in first:  # language tag like ```md / ```text
                text = text[nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if not text:
        raise HTTPException(status_code=502, detail="The model returned an empty result.")

    return PersonaEnhanceOut(
        system_prompt=text, used_provider=prov.name, used_model=model
    )


@router.patch("/{persona_id}", response_model=PersonaOut)
def update_persona(
    persona_id: str,
    payload: PersonaUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> PersonaOut:
    p = _get_owned(db, user, persona_id)
    if payload.name is not None:
        p.name = payload.name
    if payload.description is not None:
        p.description = payload.description
    if payload.system_prompt is not None:
        p.system_prompt = payload.system_prompt
    if payload.tools_enabled is not None:
        p.tools_enabled = payload.tools_enabled
    if payload.lanes is not None:
        p.lanes_json = [lane.model_dump() for lane in payload.lanes]
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.post("/{persona_id}/default", response_model=PersonaOut)
def set_default_persona(
    persona_id: str,
    payload: dict | None = None,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> PersonaOut:
    """Mark a persona as the user's default (used when starting a New chat). Passing
    {"default": false} clears it. Only one persona can be default at a time."""
    p = _get_owned(db, user, persona_id)
    make_default = True if payload is None else bool(payload.get("default", True))
    # Clear the flag on all of the user's personas, then set it on this one.
    for other in db.scalars(
        select(Persona).where(Persona.user_id == user.id)
    ).all():
        other.is_default = False
    p.is_default = make_default
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.delete("/{persona_id}", status_code=204)
def delete_persona(
    persona_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    p = _get_owned(db, user, persona_id)
    db.delete(p)
    db.commit()
