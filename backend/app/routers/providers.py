from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..crypto import decrypt, encrypt, mask_secret
from ..db import get_db
from ..models import Provider, User
from ..providers import diagnostics, oauth
from ..providers.registry import build_provider
from ..schemas import (
    OAuthCompleteRequest,
    OAuthPollOut,
    OAuthStartOut,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
    TestResult,
)
from ..security import current_user

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _serialize(p: Provider) -> ProviderOut:
    key = decrypt(p.api_key_encrypted)
    return ProviderOut(
        id=p.id,
        name=p.name,
        provider_type=p.provider_type,  # type: ignore[arg-type]
        auth_method=p.auth_method,  # type: ignore[arg-type]
        base_url=p.base_url,
        masked_key=mask_secret(key),
        has_key=bool(p.api_key_encrypted),
        oauth_connected=oauth.oauth_connected(p),
        oauth_expires_at=p.oauth_expires_at,
        oauth_pending=oauth.device_flow_pending(p),
        models=list(p.models_json or []),
        default_model=p.default_model,
        # Hide internal, underscore-prefixed state (e.g. the in-flight `_device_flow`
        # device code) from the API response — it must never reach the browser.
        extra={
            k: v for k, v in (p.extra_json or {}).items() if not k.startswith("_")
        },
        is_default=p.is_default,
        created_at=p.created_at,
    )


def _get_owned(db: DbSession, user: User, provider_id: str) -> Provider:
    p = db.get(Provider, provider_id)
    if not p or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Provider not found")
    return p


def _clear_defaults(db: DbSession, user: User) -> None:
    for other in db.scalars(
        select(Provider).where(Provider.user_id == user.id, Provider.is_default)
    ):
        other.is_default = False


@router.get("", response_model=list[ProviderOut])
def list_providers(
    user: User = Depends(current_user), db: DbSession = Depends(get_db)
) -> list[ProviderOut]:
    rows = db.scalars(
        select(Provider).where(Provider.user_id == user.id).order_by(Provider.created_at)
    ).all()
    return [_serialize(p) for p in rows]


@router.post("", response_model=ProviderOut)
def create_provider(
    payload: ProviderCreate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProviderOut:
    if payload.is_default:
        _clear_defaults(db, user)
    p = Provider(
        user_id=user.id,
        name=payload.name,
        provider_type=payload.provider_type,
        auth_method=payload.auth_method,
        base_url=payload.base_url,
        api_key_encrypted=encrypt(payload.api_key),
        models_json=payload.models or [],
        default_model=payload.default_model,
        extra_json=payload.extra or {},
        is_default=payload.is_default,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.patch("/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ProviderOut:
    p = _get_owned(db, user, provider_id)
    if payload.name is not None:
        p.name = payload.name
    if payload.base_url is not None:
        p.base_url = payload.base_url
    if payload.auth_method is not None:
        p.auth_method = payload.auth_method
    # empty/omitted api_key leaves existing key untouched
    if payload.api_key:
        p.api_key_encrypted = encrypt(payload.api_key)
    if payload.models is not None:
        p.models_json = payload.models
    if payload.default_model is not None:
        p.default_model = payload.default_model
    if payload.extra is not None:
        p.extra_json = {**(p.extra_json or {}), **payload.extra}
    if payload.is_default is not None:
        if payload.is_default:
            _clear_defaults(db, user)
        p.is_default = payload.is_default
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.delete("/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    p = _get_owned(db, user, provider_id)
    db.delete(p)
    db.commit()


@router.post("/{provider_id}/test", response_model=TestResult)
async def test_provider(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> TestResult:
    p = _get_owned(db, user, provider_id)
    try:
        llm = await build_provider(p, db)
        ok, detail = await llm.test()
        return TestResult(ok=ok, detail=detail)
    except Exception as exc:  # noqa: BLE001
        return TestResult(ok=False, detail=str(exc))


@router.get("/{provider_id}/models", response_model=list[str])
async def refresh_models(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> list[str]:
    p = _get_owned(db, user, provider_id)
    llm = await build_provider(p, db)
    models = await llm.list_models()
    if models:
        p.models_json = models
        db.add(p)
        db.commit()
    return models


def _sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/{provider_id}/test/stream")
async def test_stream(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    p = _get_owned(db, user, provider_id)

    async def gen():
        try:
            async for item in diagnostics.test_stream(p, db):
                if item.get("done"):
                    yield _sse("done", item)
                else:
                    yield _sse("step", item)
        except Exception as exc:  # noqa: BLE001
            yield _sse("done", {"done": True, "ok": False, "detail": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{provider_id}/models/stream")
async def models_stream(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    p = _get_owned(db, user, provider_id)

    async def gen():
        final_models: list[str] = []
        try:
            async for item in diagnostics.models_stream(p, db):
                if item.get("done"):
                    final_models = item.get("models") or []
                    yield _sse("done", item)
                else:
                    yield _sse("step", item)
        except Exception as exc:  # noqa: BLE001
            yield _sse("done", {"done": True, "ok": False, "detail": str(exc), "models": []})
            return
        # Persist the refreshed catalogue.
        if final_models:
            fresh = db.get(Provider, provider_id)
            if fresh:
                fresh.models_json = final_models
                db.add(fresh)
                db.commit()

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- OAuth ----------------


@router.post("/{provider_id}/oauth/start", response_model=OAuthStartOut)
async def oauth_start(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OAuthStartOut:
    p = _get_owned(db, user, provider_id)
    try:
        result = await oauth.start_flow(p, db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return OAuthStartOut(**result)


@router.post("/{provider_id}/oauth/poll", response_model=OAuthPollOut)
async def oauth_poll(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OAuthPollOut:
    p = _get_owned(db, user, provider_id)
    result = await oauth.poll_flow(p, db)
    return OAuthPollOut(**result)


@router.post("/{provider_id}/oauth/complete", response_model=OAuthPollOut)
async def oauth_complete(
    provider_id: str,
    payload: OAuthCompleteRequest,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OAuthPollOut:
    p = _get_owned(db, user, provider_id)
    if not payload.code:
        raise HTTPException(status_code=400, detail="Missing code")
    result = await oauth.complete_flow(p, db, payload.code, payload.state)
    return OAuthPollOut(**result)


@router.post("/{provider_id}/oauth/disconnect", status_code=204)
def oauth_disconnect(
    provider_id: str,
    user: User = Depends(current_user),
    db: DbSession = Depends(get_db),
):
    p = _get_owned(db, user, provider_id)
    oauth.disconnect(p, db)
