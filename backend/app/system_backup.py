from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .crypto import decrypt, encrypt
from .models import (
    Attachment,
    EvalRun,
    EvalSuite,
    Folder,
    GeneratedFile,
    Integration,
    Lane,
    LaneMessage,
    Persona,
    Provider,
    Session as ChatSession,
    Snippet,
    ToolCall,
    ToolCredential,
    Turn,
    User,
)
from .tools.artifacts import GENERATED_SUBDIR

EXPORT_VERSION = 1


def _iso(v) -> str | None:
    return v.isoformat() if v else None


def _safe_member_name(name: str | None) -> str | None:
    """Return a traversal-safe flat filename for an imported file, or ``None`` if unsafe.

    Backup archives store flat ``<uuid>.<ext>`` names. Anything containing a path
    separator, drive/UNC prefix, or ``..`` is rejected to prevent Zip-Slip path traversal
    when restoring files under ``UPLOAD_DIR``.
    """
    if not name:
        return None
    normalized = str(name).replace("\\", "/").strip()
    if not normalized or normalized in (".", ".."):
        return None
    if "/" in normalized or ":" in normalized:
        return None
    base = os.path.basename(normalized)
    if not base or base != normalized or base.startswith("."):
        return None
    return base


# --------------------------------------------------------------------------- export


def build_export(db: DbSession, user: User) -> bytes:
    """Serialize everything belonging to a user into a portable ZIP.

    Secrets (API keys, OAuth tokens) are DECRYPTED into the export so the backup can be
    restored on another instance/encryption key. The ZIP therefore contains sensitive
    plaintext — treat it as a secret.
    """
    data: dict = {}

    data["user"] = {
        "email": user.email,
        "custom_instructions": user.custom_instructions,
    }

    providers = db.scalars(select(Provider).where(Provider.user_id == user.id)).all()
    data["providers"] = [
        {
            "id": p.id,
            "name": p.name,
            "provider_type": p.provider_type,
            "auth_method": p.auth_method,
            "base_url": p.base_url,
            "api_key": decrypt(p.api_key_encrypted),
            "oauth_access_token": decrypt(p.oauth_access_token_encrypted),
            "oauth_refresh_token": decrypt(p.oauth_refresh_token_encrypted),
            "oauth_expires_at": p.oauth_expires_at,
            "models_json": p.models_json,
            "default_model": p.default_model,
            "extra_json": p.extra_json,
            "is_default": p.is_default,
            "created_at": _iso(p.created_at),
        }
        for p in providers
    ]

    creds = db.scalars(
        select(ToolCredential).where(ToolCredential.user_id == user.id)
    ).all()
    data["tool_credentials"] = [
        {
            "id": c.id,
            "tool": c.tool,
            "api_key": decrypt(c.api_key_encrypted),
            "extra_json": c.extra_json,
        }
        for c in creds
    ]

    personas = db.scalars(select(Persona).where(Persona.user_id == user.id)).all()
    data["personas"] = [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "system_prompt": p.system_prompt,
            "tools_enabled": p.tools_enabled,
            "is_default": p.is_default,
            "lanes_json": p.lanes_json,
        }
        for p in personas
    ]

    folders = db.scalars(select(Folder).where(Folder.user_id == user.id)).all()
    data["folders"] = [
        {"id": f.id, "name": f.name, "position": f.position} for f in folders
    ]

    snippets = db.scalars(select(Snippet).where(Snippet.user_id == user.id)).all()
    data["snippets"] = [
        {"id": s.id, "title": s.title, "content": s.content} for s in snippets
    ]

    suites = db.scalars(select(EvalSuite).where(EvalSuite.user_id == user.id)).all()
    data["eval_suites"] = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "system_prompt": s.system_prompt,
            "prompts_json": s.prompts_json,
            "models_json": s.models_json,
        }
        for s in suites
    ]
    runs = db.scalars(select(EvalRun).where(EvalRun.user_id == user.id)).all()
    data["eval_runs"] = [
        {
            "id": r.id,
            "suite_id": r.suite_id,
            "results_json": r.results_json,
            "summary_json": r.summary_json,
            "created_at": _iso(r.created_at),
        }
        for r in runs
    ]

    integrations = db.scalars(
        select(Integration).where(Integration.user_id == user.id)
    ).all()
    data["integrations"] = [
        {
            "id": i.id,
            "kind": i.kind,
            "enabled": i.enabled,
            "eula_accepted": i.eula_accepted,
            "command": i.command,
            "args_json": i.args_json,
        }
        for i in integrations
    ]

    # Sessions + all nested rows.
    sessions = db.scalars(
        select(ChatSession).where(ChatSession.user_id == user.id)
    ).all()
    sess_out = []
    for s in sessions:
        lanes = db.scalars(select(Lane).where(Lane.session_id == s.id)).all()
        turns = db.scalars(select(Turn).where(Turn.session_id == s.id)).all()
        lane_ids = [l.id for l in lanes]
        msgs = (
            db.scalars(select(LaneMessage).where(LaneMessage.lane_id.in_(lane_ids))).all()
            if lane_ids
            else []
        )
        msg_ids = [m.id for m in msgs]
        tcs = (
            db.scalars(select(ToolCall).where(ToolCall.lane_message_id.in_(msg_ids))).all()
            if msg_ids
            else []
        )
        sess_out.append(
            {
                "id": s.id,
                "title": s.title,
                "system_prompt": s.system_prompt,
                "tools_enabled": s.tools_enabled,
                "tool_config_json": s.tool_config_json,
                "folder_id": s.folder_id,
                "pinned": s.pinned,
                "archived": s.archived,
                "trashed": s.trashed,
                "created_at": _iso(s.created_at),
                "updated_at": _iso(s.updated_at),
                "lanes": [
                    {
                        "id": l.id,
                        "provider_id": l.provider_id,
                        "model": l.model,
                        "position": l.position,
                        "role": l.role,
                        "state": "idle",
                        "hidden": l.hidden,
                    }
                    for l in lanes
                ],
                "turns": [
                    {
                        "id": t.id,
                        "order_index": t.order_index,
                        "content": t.content,
                        "target_lane_ids_json": t.target_lane_ids_json,
                        "created_at": _iso(t.created_at),
                    }
                    for t in turns
                ],
                "messages": [
                    {
                        "id": m.id,
                        "lane_id": m.lane_id,
                        "turn_id": m.turn_id,
                        "role": m.role,
                        "content": m.content,
                        "order_index": m.order_index,
                        "usage_json": m.usage_json,
                        "latency_ms": m.latency_ms,
                        "cost_usd": m.cost_usd,
                        "error": m.error,
                        "created_at": _iso(m.created_at),
                    }
                    for m in msgs
                ],
                "tool_calls": [
                    {
                        "id": tc.id,
                        "lane_message_id": tc.lane_message_id,
                        "tool_name": tc.tool_name,
                        "arguments_json": tc.arguments_json,
                        "result_json": tc.result_json,
                        "citations_json": tc.citations_json,
                        "status": tc.status,
                    }
                    for tc in tcs
                ],
            }
        )
    data["sessions"] = sess_out

    attachments = db.scalars(
        select(Attachment).where(Attachment.user_id == user.id)
    ).all()
    data["attachments"] = [
        {
            "id": a.id,
            "turn_id": a.turn_id,
            "kind": a.kind,
            "filename": a.filename,
            "mime_type": a.mime_type,
            "size_bytes": a.size_bytes,
            "storage_path": a.storage_path,
            "extracted_text": a.extracted_text,
        }
        for a in attachments
    ]

    gen_files = db.scalars(
        select(GeneratedFile).where(GeneratedFile.user_id == user.id)
    ).all()
    data["generated_files"] = [
        {
            "id": g.id,
            "session_id": g.session_id,
            "stored_name": g.stored_name,
            "download_name": g.download_name,
            "mime_type": g.mime_type,
            "size_bytes": g.size_bytes,
            "kind": g.kind,
        }
        for g in gen_files
    ]

    manifest = {
        "version": EXPORT_VERSION,
        "app": "MultiChat",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_email": user.email,
        "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)},
        "contains_secrets": True,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=2))
        # Bundle uploaded attachment files.
        for a in attachments:
            path = os.path.join(settings.UPLOAD_DIR, a.storage_path)
            if os.path.exists(path):
                z.write(path, f"uploads/{a.storage_path}")
        # Bundle generated files.
        for g in gen_files:
            path = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR, g.stored_name)
            if os.path.exists(path):
                z.write(path, f"generated/{g.stored_name}")

    return buf.getvalue()


# --------------------------------------------------------------------------- import


def _delete_user_data(db: DbSession, user: User) -> None:
    """Remove all of a user's data (keeps the User row itself)."""
    # Sessions cascade to lanes/turns/messages/tool_calls/attachments(turn-linked).
    for s in db.scalars(select(ChatSession).where(ChatSession.user_id == user.id)).all():
        db.delete(s)
    for a in db.scalars(select(Attachment).where(Attachment.user_id == user.id)).all():
        db.delete(a)
    for g in db.scalars(
        select(GeneratedFile).where(GeneratedFile.user_id == user.id)
    ).all():
        db.delete(g)
    for r in db.scalars(select(EvalRun).where(EvalRun.user_id == user.id)).all():
        db.delete(r)
    for s in db.scalars(select(EvalSuite).where(EvalSuite.user_id == user.id)).all():
        db.delete(s)
    for p in db.scalars(select(Persona).where(Persona.user_id == user.id)).all():
        db.delete(p)
    for f in db.scalars(select(Folder).where(Folder.user_id == user.id)).all():
        db.delete(f)
    for s in db.scalars(select(Snippet).where(Snippet.user_id == user.id)).all():
        db.delete(s)
    for i in db.scalars(select(Integration).where(Integration.user_id == user.id)).all():
        db.delete(i)
    for c in db.scalars(
        select(ToolCredential).where(ToolCredential.user_id == user.id)
    ).all():
        db.delete(c)
    for p in db.scalars(select(Provider).where(Provider.user_id == user.id)).all():
        db.delete(p)
    db.flush()


def restore_import(db: DbSession, user: User, zip_bytes: bytes) -> dict:
    """Wipe the user's data and restore everything from a backup ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Not a valid ZIP file")
    names = set(zf.namelist())
    if "data.json" not in names:
        raise ValueError("Backup is missing data.json")
    data = json.loads(zf.read("data.json").decode("utf-8"))

    _delete_user_data(db, user)

    # User settings.
    u = data.get("user") or {}
    if "custom_instructions" in u:
        user.custom_instructions = u.get("custom_instructions")
        db.add(user)

    uid = user.id

    for p in data.get("providers", []):
        db.add(
            Provider(
                id=p["id"],
                user_id=uid,
                name=p["name"],
                provider_type=p["provider_type"],
                auth_method=p.get("auth_method", "api_key"),
                base_url=p.get("base_url"),
                api_key_encrypted=encrypt(p.get("api_key")),
                oauth_access_token_encrypted=encrypt(p.get("oauth_access_token")),
                oauth_refresh_token_encrypted=encrypt(p.get("oauth_refresh_token")),
                oauth_expires_at=p.get("oauth_expires_at"),
                models_json=p.get("models_json") or [],
                default_model=p.get("default_model"),
                extra_json=p.get("extra_json") or {},
                is_default=bool(p.get("is_default")),
            )
        )

    for c in data.get("tool_credentials", []):
        db.add(
            ToolCredential(
                id=c["id"],
                user_id=uid,
                tool=c["tool"],
                api_key_encrypted=encrypt(c.get("api_key")),
                extra_json=c.get("extra_json") or {},
            )
        )

    for p in data.get("personas", []):
        db.add(
            Persona(
                id=p["id"],
                user_id=uid,
                name=p["name"],
                description=p.get("description"),
                system_prompt=p.get("system_prompt"),
                tools_enabled=bool(p.get("tools_enabled")),
                is_default=bool(p.get("is_default")),
                lanes_json=p.get("lanes_json") or [],
            )
        )

    for f in data.get("folders", []):
        db.add(
            Folder(id=f["id"], user_id=uid, name=f["name"], position=f.get("position", 0))
        )

    for s in data.get("snippets", []):
        db.add(
            Snippet(id=s["id"], user_id=uid, title=s["title"], content=s.get("content", ""))
        )

    for s in data.get("eval_suites", []):
        db.add(
            EvalSuite(
                id=s["id"],
                user_id=uid,
                name=s["name"],
                description=s.get("description"),
                system_prompt=s.get("system_prompt"),
                prompts_json=s.get("prompts_json") or [],
                models_json=s.get("models_json") or [],
            )
        )
    for r in data.get("eval_runs", []):
        db.add(
            EvalRun(
                id=r["id"],
                suite_id=r["suite_id"],
                user_id=uid,
                results_json=r.get("results_json") or [],
                summary_json=r.get("summary_json") or {},
            )
        )

    for i in data.get("integrations", []):
        db.add(
            Integration(
                id=i["id"],
                user_id=uid,
                kind=i["kind"],
                enabled=bool(i.get("enabled")),
                eula_accepted=bool(i.get("eula_accepted")),
                command=i.get("command"),
                args_json=i.get("args_json") or [],
            )
        )

    db.flush()

    # Sessions and nested rows (order: session -> lanes -> turns -> messages -> tool_calls).
    for s in data.get("sessions", []):
        db.add(
            ChatSession(
                id=s["id"],
                user_id=uid,
                title=s.get("title", "New topic"),
                system_prompt=s.get("system_prompt"),
                tools_enabled=bool(s.get("tools_enabled")),
                tool_config_json=s.get("tool_config_json") or {},
                folder_id=s.get("folder_id"),
                pinned=bool(s.get("pinned")),
                archived=bool(s.get("archived")),
                trashed=bool(s.get("trashed")),
            )
        )
        db.flush()
        for l in s.get("lanes", []):
            db.add(
                Lane(
                    id=l["id"],
                    session_id=s["id"],
                    provider_id=l["provider_id"],
                    model=l["model"],
                    position=l.get("position", 0),
                    role=l.get("role", "responder"),
                    state="idle",
                    hidden=bool(l.get("hidden")),
                )
            )
        for t in s.get("turns", []):
            db.add(
                Turn(
                    id=t["id"],
                    session_id=s["id"],
                    order_index=t.get("order_index", 0),
                    content=t.get("content", ""),
                    target_lane_ids_json=t.get("target_lane_ids_json"),
                )
            )
        db.flush()
        for m in s.get("messages", []):
            db.add(
                LaneMessage(
                    id=m["id"],
                    lane_id=m["lane_id"],
                    turn_id=m["turn_id"],
                    role=m.get("role", "assistant"),
                    content=m.get("content", ""),
                    order_index=m.get("order_index", 0),
                    usage_json=m.get("usage_json"),
                    latency_ms=m.get("latency_ms"),
                    cost_usd=m.get("cost_usd"),
                    error=m.get("error"),
                )
            )
        db.flush()
        for tc in s.get("tool_calls", []):
            db.add(
                ToolCall(
                    id=tc["id"],
                    lane_message_id=tc["lane_message_id"],
                    tool_name=tc["tool_name"],
                    arguments_json=tc.get("arguments_json") or {},
                    result_json=tc.get("result_json"),
                    citations_json=tc.get("citations_json"),
                    status=tc.get("status", "ok"),
                )
            )

    # Attachments (metadata + restore files).
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    for a in data.get("attachments", []):
        safe = _safe_member_name(a.get("storage_path"))
        if not safe:
            continue  # skip traversal / malformed entries (Zip-Slip guard)
        db.add(
            Attachment(
                id=a["id"],
                turn_id=a.get("turn_id"),
                user_id=uid,
                kind=a.get("kind", "image"),
                filename=a["filename"],
                mime_type=a["mime_type"],
                size_bytes=a.get("size_bytes", 0),
                storage_path=safe,
                extracted_text=a.get("extracted_text"),
            )
        )
        arc = f"uploads/{safe}"
        if arc in names:
            dest = os.path.join(settings.UPLOAD_DIR, safe)
            with open(dest, "wb") as fh:
                fh.write(zf.read(arc))

    # Generated files (metadata + restore files).
    gen_dir = os.path.join(settings.UPLOAD_DIR, GENERATED_SUBDIR)
    os.makedirs(gen_dir, exist_ok=True)
    for g in data.get("generated_files", []):
        safe = _safe_member_name(g.get("stored_name"))
        if not safe:
            continue  # skip traversal / malformed entries (Zip-Slip guard)
        db.add(
            GeneratedFile(
                id=g["id"],
                user_id=uid,
                session_id=g.get("session_id"),
                stored_name=safe,
                download_name=g["download_name"],
                mime_type=g.get("mime_type", "application/octet-stream"),
                size_bytes=g.get("size_bytes", 0),
                kind=g.get("kind", "file"),
            )
        )
        arc = f"generated/{safe}"
        if arc in names:
            with open(os.path.join(gen_dir, safe), "wb") as fh:
                fh.write(zf.read(arc))

    db.commit()

    return {
        k: len(v)
        for k, v in data.items()
        if isinstance(v, list)
    }
