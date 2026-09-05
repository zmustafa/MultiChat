from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import stat
import tempfile
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, inspect, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session as DbSession

from .config import settings
from .crypto import decrypt, encrypt
from .models import (
    AnswerSnapshot,
    Attachment,
    DeliberationRun,
    DeliberationStep,
    EvalRun,
    EvalSuite,
    Folder,
    GeneratedFile,
    Integration,
    Lane,
    LaneMessage,
    Persona,
    Provider,
    Snippet,
    StarterPersonaState,
    ToolCall,
    ToolCredential,
    Turn,
    User,
)
from .models import (
    Session as ChatSession,
)
from .tools.artifacts import GENERATED_SUBDIR

EXPORT_VERSION = 2

# Backup-wide limits, deliberately much larger than individual upload limits. Highly
# compressible transcripts are legitimate, so bound expanded bytes rather than ratios.
# Keep these module constants configurable independently of MAX_UPLOAD_BYTES.
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_BACKUP_MEMBERS = 100_000
_COPY_CHUNK = 1024 * 1024
_IMPORT_LOCK = Lock()


def _iso(v) -> str | None:
    return v.isoformat() if v else None


def _timestamp_kwargs(data: dict, *fields: str) -> dict:
    """Parse optional ISO timestamps from current or older backup formats."""
    out = {}
    for field in fields:
        raw = data.get(field)
        if not raw:
            continue
        try:
            out[field] = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
    return out


def _safe_member_name(name: str | None) -> str | None:
    """Return a traversal-safe flat filename for an imported file, or ``None`` if unsafe.

    Backup archives store flat ``<uuid>.<ext>`` names. Anything containing a path
    separator, drive/UNC prefix, or hidden/reserved basename is rejected to prevent traversal
    when restoring files under ``UPLOAD_DIR``.
    """
    if not isinstance(name, str) or not name or name != name.strip():
        return None
    try:
        if len(name.encode("utf-8")) > 255:
            return None
    except UnicodeError:
        return None
    if name.startswith(".") or name.endswith("."):
        return None
    if any(ord(c) < 32 or c in '/\\:<>"|?*' for c in name):
        return None
    # Windows aliases include device names even with an extension, trailing dots /
    # spaces, alternate data streams and case variants. Reject them on every OS so a
    # portable backup cannot become dangerous when moved to Windows.
    stem = name.split(".", 1)[0].rstrip().upper()
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")
    if stem in devices or name.casefold() in {GENERATED_SUBDIR.casefold(), "runs"}:
        return None
    return name


_TABLES = {
    "providers": Provider, "tool_credentials": ToolCredential, "personas": Persona,
    "starter_persona_states": StarterPersonaState, "folders": Folder, "snippets": Snippet,
    "eval_suites": EvalSuite, "eval_runs": EvalRun, "integrations": Integration,
    "sessions": ChatSession, "attachments": Attachment, "generated_files": GeneratedFile,
    "answer_snapshots": AnswerSnapshot, "deliberation_runs": DeliberationRun,
    "deliberation_steps": DeliberationStep,
}
_NESTED_TABLES = {"lanes": Lane, "turns": Turn, "messages": LaneMessage, "tool_calls": ToolCall}
_JSON_LISTS = {
    "models_json", "lanes_json", "prompts_json", "results_json", "args_json",
    "target_lane_ids_json", "citations_json", "convergence_json",
}


def _invalid(detail: str) -> ValueError:
    # Never put untrusted values (which may include credentials) into API errors.
    return ValueError(f"Invalid backup: {detail}")


def _json_object(pairs: list) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _invalid("duplicate JSON key")
        obj[key] = value
    return obj


def _bad_constant(_value: str):
    raise _invalid("non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _invalid("non-finite JSON number")
    return number


def _unicode_strings(value) -> None:
    if isinstance(value, str):
        value.encode("utf-8")  # reject unpaired surrogates before SQL/filesystem calls
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode_strings(key)
            _unicode_strings(item)
    elif isinstance(value, list):
        for item in value:
            _unicode_strings(item)


def _read_json(zf: zipfile.ZipFile, name: str):
    if zf.getinfo(name).file_size > MAX_JSON_BYTES:
        raise _invalid("JSON document exceeds size limit")
    try:
        value = json.loads(zf.read(name).decode("utf-8"), object_pairs_hook=_json_object,
                   parse_constant=_bad_constant, parse_float=_finite_float)
        _unicode_strings(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _invalid("malformed JSON document") from exc


def _check_archive(zf: zipfile.ZipFile) -> set[str]:
    infos = zf.infolist()
    if len(infos) > MAX_BACKUP_MEMBERS:
        raise _invalid("too many ZIP members")
    names: set[str] = set()
    canonical_names: dict[str, str] = {}
    expanded = 0
    for info in infos:
        name = info.filename
        if ((name in names and name in {"data.json", "manifest.json", "uploads/", "generated/"})
                or name != info.orig_filename):
            raise _invalid("duplicate or malformed ZIP member")
        canonical = name.casefold()
        if canonical in canonical_names and canonical_names[canonical] != name:
            raise _invalid("aliased ZIP member names")
        canonical_names[canonical] = name
        names.add(name)
        if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
            raise _invalid("encrypted or symbolic-link ZIP member")
        # These are the application's export formats. ZipExtFile does not bound a
        # single BZIP2/LZMA decompression call's output, even when read(n) is bounded.
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _invalid("unsupported ZIP compression")
        if name not in {"manifest.json", "data.json", "uploads/", "generated/"}:
            prefix, sep, base = name.partition("/")
            if not sep or prefix not in {"uploads", "generated"} or not _safe_member_name(base):
                raise _invalid("unsafe ZIP member path")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise _invalid("expanded ZIP exceeds size limit")
    if not {"manifest.json", "data.json"} <= names:
        raise _invalid("missing manifest.json or data.json")
    return names


def _validate_row(row: dict, model, *, implicit: tuple[str, ...] = ()) -> None:
    if not isinstance(row, dict):
        raise _invalid("records must be objects")
    for column in model.__table__.columns:
        field = column.name
        if field == "user_id" or field in implicit:
            continue
        # Secrets have plaintext names in the portable format.
        if field.endswith("_encrypted"):
            field = field.removesuffix("_encrypted")
        if field not in row:
            if column.primary_key or (not column.nullable and column.default is None):
                raise _invalid(f"missing {model.__tablename__}.{field}")
            continue
        value = row[field]
        if value is None:
            if not column.nullable:
                raise _invalid(f"null {model.__tablename__}.{field}")
            continue
        kind = column.type
        valid = True
        if isinstance(kind, Boolean):
            valid = type(value) is bool
        elif isinstance(kind, Integer):
            valid = type(value) is int and -(2**63) <= value < 2**63
        elif isinstance(kind, Float):
            valid = type(value) in (int, float)
            if valid:
                try:
                    valid = math.isfinite(value)
                except OverflowError:
                    valid = False
        elif isinstance(kind, String):
            valid = isinstance(value, str)
        elif isinstance(kind, DateTime):
            valid = isinstance(value, str)
            if valid:
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    valid = False
        elif isinstance(kind, JSON):
            valid = isinstance(value, list if field in _JSON_LISTS else dict)
        if not valid or (column.primary_key and not value):
            raise _invalid(f"invalid type or value for {model.__tablename__}.{field}")


def _ref(row: dict, field: str, targets: dict, *, optional: bool = False):
    value = row.get(field)
    if value is None and optional:
        return None
    if not isinstance(value, str) or value not in targets:
        raise _invalid(f"{field} must reference an imported record in the same graph")
    return targets[value]


def _validate_graph(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
        raise _invalid("data and user must be objects")
    _validate_row(data["user"], User, implicit=("id", "email", "password_hash", "created_at"))
    rows = {}
    for key, model in {**_TABLES, **_NESTED_TABLES}.items():
        items = data.get(key, []) if key in _TABLES else []
        if not isinstance(items, list):
            raise _invalid(f"{key} must be a list")
        rows[key] = {}
        for row in items:
            _validate_row(row, model)
            pk = "seed_key" if model is StarterPersonaState else "id"
            if row[pk] in rows[key]:
                raise _invalid(f"duplicate id in {key}")
            rows[key][row[pk]] = row
    for session in rows["sessions"].values():
        for key, model in _NESTED_TABLES.items():
            items = session.get(key, [])
            if not isinstance(items, list):
                raise _invalid(f"session {key} must be a list")
            for row in items:
                _validate_row(row, model, implicit=("session_id",))
                if row["id"] in rows[key]:
                    raise _invalid(f"duplicate id in {key}")
                rows[key][row["id"]] = row
        _ref(session, "folder_id", rows["folders"], optional=True)
    local_graphs = {}
    for session in rows["sessions"].values():
        local = {key: {r["id"]: r for r in session.get(key, [])} for key in _NESTED_TABLES}
        local_graphs[session["id"]] = local
        for lane in local["lanes"].values():
            _ref(lane, "provider_id", rows["providers"])
        for turn in local["turns"].values():
            for lane_id in turn.get("target_lane_ids_json") or []:
                _ref({"lane_id": lane_id}, "lane_id", local["lanes"])
        for message in local["messages"].values():
            _ref(message, "lane_id", local["lanes"])
            _ref(message, "turn_id", local["turns"])
        for call in local["tool_calls"].values():
            _ref(call, "lane_message_id", local["messages"])
    for key, field, target, optional in (
        ("starter_persona_states", "persona_id", "personas", True),
        ("eval_runs", "suite_id", "eval_suites", False),
        ("attachments", "turn_id", "turns", True),
        ("generated_files", "session_id", "sessions", True),
        ("answer_snapshots", "session_id", "sessions", True),
    ):
        for row in rows[key].values():
            _ref(row, field, rows[target], optional=optional)
    for key, field in (("personas", "lanes_json"), ("eval_suites", "models_json")):
        for row in rows[key].values():
            for spec in row.get(field) or []:
                if not isinstance(spec, dict) or not isinstance(spec.get("model"), str):
                    raise _invalid("invalid model template")
                # Starter templates use an empty id until a provider is configured.
                if spec.get("provider_id") != "":
                    _ref(spec, "provider_id", rows["providers"])
    for run in rows["deliberation_runs"].values():
        session = _ref(run, "session_id", rows["sessions"])
        local = local_graphs[session["id"]]
        _ref(run, "turn_id", local["turns"])
        local_lanes = local["lanes"]
        _ref(run.get("config_json") or {}, "judge_lane_id", local_lanes, optional=True)
        _ref(run.get("config_json") or {}, "parent_run_id", rows["deliberation_runs"], optional=True)
        _ref(run.get("vote_json") or {}, "winner_lane_id", local_lanes, optional=True)
    for step in rows["deliberation_steps"].values():
        run = _ref(step, "run_id", rows["deliberation_runs"])
        local = local_graphs[run["session_id"]]
        _ref(step, "lane_id", local["lanes"], optional=True)
        message = _ref(step, "message_id", local["messages"], optional=True)
        if message and (message["turn_id"] != run["turn_id"] or
                        (step.get("lane_id") is not None and message["lane_id"] != step["lane_id"])):
            raise _invalid("deliberation message does not belong to its turn/lane")
    return rows


def _validate_ownership(db: DbSession, user: User, rows: dict) -> None:
    for key, model in {**_TABLES, **_NESTED_TABLES}.items():
        if model is StarterPersonaState:
            continue  # composite key is scoped to the restoring user
        query = select(model.id)
        if model in (Lane, Turn):
            query = query.join(ChatSession, model.session_id == ChatSession.id)
            owner = ChatSession.user_id
        elif model in (LaneMessage, ToolCall):
            if model is ToolCall:
                query = query.join(LaneMessage, ToolCall.lane_message_id == LaneMessage.id)
            query = query.join(Lane, LaneMessage.lane_id == Lane.id).join(ChatSession)
            owner = ChatSession.user_id
        elif model is DeliberationStep:
            query = query.join(DeliberationRun)
            owner = DeliberationRun.user_id
        else:
            owner = model.user_id
        ids = list(rows[key])
        # Stay below SQLite's bind parameter limit, including for large transcripts.
        for start in range(0, len(ids), 400):
            if db.scalar(query.where(model.id.in_(ids[start:start + 400]), owner != user.id).limit(1)):
                raise _invalid("record id belongs to another user")


def _regular_path(path: Path) -> None:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise _invalid("symbolic-link storage destination")
    if path.exists() and not path.is_file():
        raise _invalid("storage destination is not a regular file")


def _file_targets(db: DbSession, user: User, data: dict) -> list[tuple[str, Path]]:
    root = Path(settings.UPLOAD_DIR).resolve()
    targets = []
    for key, model, field, prefix, directory in (
        ("attachments", Attachment, "storage_path", "uploads", root),
        ("generated_files", GeneratedFile, "stored_name", "generated", root / GENERATED_SUBDIR),
    ):
        records = data.get(key, [])
        if not records:
            continue
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise _invalid("symbolic-link storage directory")
        if directory.exists() and not directory.is_dir():
            raise _invalid("storage directory is not a directory")
        owners: dict[str, set[str]] = {}
        for name, owner in db.execute(select(getattr(model, field), model.user_id)):
            owners.setdefault(name.casefold(), set()).add(owner)
        seen: dict[str, str] = {}
        for row in records:
            name = _safe_member_name(row.get(field))
            if not name:
                raise _invalid("unsafe storage filename")
            canonical = name.casefold()
            if canonical in seen:
                if seen[canonical] != name:
                    raise _invalid("aliased storage filenames")
                continue  # cloned sessions legitimately share an attachment file
            seen[canonical] = name
            file_owners = owners.get(canonical, set())
            if file_owners - {user.id}:
                raise _invalid("storage filename belongs to another user")
            path = directory / name
            _regular_path(path)
            if path.exists() and file_owners != {user.id}:
                raise _invalid("storage filename is already in use")
            targets.append((f"{prefix}/{name}", path))
    return targets


def _stage_members(zf: zipfile.ZipFile, stage: Path, targets: list) -> list:
    wanted = dict(targets)
    staged = []
    expanded = 0
    digests: dict[str, bytes] = {}
    # Read *every* member to EOF to check CRCs, including unreferenced files. No final
    # destination is touched until the entire archive has been verified.
    for index, info in enumerate(zf.infolist()):
        temporary = stage / str(index)
        first = info.filename not in digests
        output = open(temporary, "xb") if first and info.filename in wanted else None
        digest = hashlib.sha256()
        try:
            with zf.open(info) as source:
                while True:
                    chunk = source.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    expanded += len(chunk)
                    if expanded > MAX_EXPANDED_BYTES:
                        raise _invalid("expanded ZIP exceeds size limit")
                    digest.update(chunk)
                    if output is not None:
                        output.write(chunk)
        finally:
            if output is not None:
                output.close()
        fingerprint = digest.digest()
        if not first and digests[info.filename] != fingerprint:
            raise _invalid("conflicting duplicate ZIP members")
        digests[info.filename] = fingerprint
        if first and info.filename in wanted:
            staged.append((temporary, wanted[info.filename]))
    return staged


def _close_export_graph(data: dict) -> None:
    """Detach obsolete *soft* links in exported copies, never in the live rows.

    Snapshots, starter tombstones and panel history intentionally survive deletion of
    their source. Templates retain model hints with the existing empty-provider marker.
    This makes new exports closed graphs without discarding that historical content.
    """
    ids = {key: {r["id"] for r in data[key]} for key in (
        "providers", "personas", "folders", "sessions", "deliberation_runs")}
    for key, field in (("personas", "lanes_json"), ("eval_suites", "models_json")):
        for row in data[key]:
            # JSON columns are shared Python objects: copy the nested dictionaries
            # before changing any references in the export.
            row[field] = [dict(spec, provider_id=(spec.get("provider_id")
                          if spec.get("provider_id") in ids["providers"] else ""))
                          for spec in row[field]]
    for key, field, target in (
        ("starter_persona_states", "persona_id", "personas"),
        ("answer_snapshots", "session_id", "sessions"),
        ("sessions", "folder_id", "folders"),
    ):
        for row in data[key]:
            if row.get(field) not in ids[target]:
                row[field] = None
    sessions = {s["id"]: s for s in data["sessions"]}
    runs = {r["id"]: r for r in data["deliberation_runs"]}
    local_graphs = {s["id"]: {"lanes": {l["id"] for l in s["lanes"]},
                                "messages": {m["id"]: m for m in s["messages"]}}
                    for s in sessions.values()}
    for session in sessions.values():
        lanes = local_graphs[session["id"]]["lanes"]
        for turn in session["turns"]:
            if turn["target_lane_ids_json"] is not None:
                turn["target_lane_ids_json"] = [i for i in turn["target_lane_ids_json"] if i in lanes]
    for run in runs.values():
        lanes = local_graphs[run["session_id"]]["lanes"]
        run["config_json"] = dict(run["config_json"] or {})
        run["vote_json"] = dict(run["vote_json"] or {})
        for obj, field, allowed in ((run["config_json"], "judge_lane_id", lanes),
                                    (run["config_json"], "parent_run_id", ids["deliberation_runs"]),
                                    (run["vote_json"], "winner_lane_id", lanes)):
            if field in obj and obj[field] not in allowed:
                obj[field] = None
    for step in data["deliberation_steps"]:
        run = runs[step["run_id"]]
        local = local_graphs[run["session_id"]]
        if step["lane_id"] not in local["lanes"]:
            step["lane_id"] = None
        message = local["messages"].get(step["message_id"])
        if not message or message["turn_id"] != run["turn_id"] or (
            step["lane_id"] and message["lane_id"] != step["lane_id"]
        ):
            step["message_id"] = None


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
        "new_chat_use_default_persona": user.new_chat_use_default_persona,
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
            "created_at": _iso(c.created_at),
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
            "notice": p.notice,
            "tools_enabled": p.tools_enabled,
            "tool_config_json": p.tool_config_json,
            "is_default": p.is_default,
            "lanes_json": p.lanes_json,
            "deliberation_json": p.deliberation_json,
            "created_at": _iso(p.created_at),
            "updated_at": _iso(p.updated_at),
        }
        for p in personas
    ]
    starter_states = db.scalars(
        select(StarterPersonaState).where(StarterPersonaState.user_id == user.id)
    ).all()
    data["starter_persona_states"] = [
        {
            "seed_key": state.seed_key,
            "persona_id": state.persona_id,
            "version": state.version,
            "seeded_hash": state.seeded_hash,
            "dismissed": state.dismissed,
        }
        for state in starter_states
    ]

    folders = db.scalars(select(Folder).where(Folder.user_id == user.id)).all()
    data["folders"] = [
        {"id": f.id, "name": f.name, "position": f.position, "created_at": _iso(f.created_at)}
        for f in folders
    ]

    snippets = db.scalars(select(Snippet).where(Snippet.user_id == user.id)).all()
    data["snippets"] = [
        {"id": s.id, "title": s.title, "content": s.content, "created_at": _iso(s.created_at)}
        for s in snippets
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
            "created_at": _iso(s.created_at),
            "updated_at": _iso(s.updated_at),
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
            "created_at": _iso(i.created_at),
            "updated_at": _iso(i.updated_at),
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
                "notice": s.notice,
                "tools_enabled": s.tools_enabled,
                "tool_config_json": s.tool_config_json,
                "folder_id": s.folder_id,
                "pinned": s.pinned,
                "archived": s.archived,
                "trashed": s.trashed,
                "mode": s.mode,
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
                        "created_at": _iso(l.created_at),
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
                        "ttft_ms": m.ttft_ms,
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
                        "created_at": _iso(tc.created_at),
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
            "created_at": _iso(a.created_at),
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
            "created_at": _iso(g.created_at),
        }
        for g in gen_files
    ]

    snapshots = db.scalars(
        select(AnswerSnapshot).where(AnswerSnapshot.user_id == user.id)
    ).all()
    data["answer_snapshots"] = [
        {
            "id": s.id,
            "session_id": s.session_id,
            "prompt": s.prompt,
            "model": s.model,
            "provider_name": s.provider_name,
            "content": s.content,
            "label": s.label,
            "created_at": _iso(s.created_at),
        }
        for s in snapshots
    ]

    deliberation_runs = db.scalars(
        select(DeliberationRun).where(DeliberationRun.user_id == user.id)
    ).all()
    run_ids = [r.id for r in deliberation_runs]
    deliberation_steps = (
        db.scalars(select(DeliberationStep).where(DeliberationStep.run_id.in_(run_ids))).all()
        if run_ids
        else []
    )
    data["deliberation_runs"] = [
        {
            "id": r.id,
            "session_id": r.session_id,
            "turn_id": r.turn_id,
            "status": r.status,
            "prompt": r.prompt,
            "rounds_used": r.rounds_used,
            "converged": r.converged,
            "config_json": r.config_json,
            "convergence_json": r.convergence_json,
            "vote_json": r.vote_json,
            "metrics_json": r.metrics_json,
            "synthesis": r.synthesis,
            "minority_report": r.minority_report,
            "extraction_json": r.extraction_json,
            "synthesis_critique_json": r.synthesis_critique_json,
            "total_calls": r.total_calls,
            "wall_ms": r.wall_ms,
            "error": r.error,
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
        }
        for r in deliberation_runs
    ]
    data["deliberation_steps"] = [
        {
            "id": s.id,
            "run_id": s.run_id,
            "lane_id": s.lane_id,
            "message_id": s.message_id,
            "round_index": s.round_index,
            "phase": s.phase,
            "label": s.label,
            "model": s.model,
            "provider_name": s.provider_name,
            "verdict": s.verdict,
            "input_json": s.input_json,
            "output_json": s.output_json,
            "raw_text": s.raw_text,
            "degraded": s.degraded,
            "error": s.error,
            "latency_ms": s.latency_ms,
            "usage_json": s.usage_json,
            "created_at": _iso(s.created_at),
        }
        for s in deliberation_steps
    ]

    _close_export_graph(data)
    manifest = {
        "version": EXPORT_VERSION,
        "app": "MultiChat",
        "exported_at": datetime.now(UTC).isoformat(),
        "user_email": user.email,
        "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)},
        "contains_secrets": True,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=2))
        # Validate paths on export too, and write shared attachment bytes only once.
        for arc, path in _file_targets(db, user, data):
            if path.exists():
                z.write(path, arc)

    return buf.getvalue()


# --------------------------------------------------------------------------- import


def _delete_user_data(db: DbSession, user: User) -> None:
    """Remove all of a user's data (keeps the User row itself)."""
    # Delete through the ORM before session cascades, including already-loaded steps.
    for run in db.scalars(select(DeliberationRun).where(DeliberationRun.user_id == user.id)).all():
        db.delete(run)
    db.flush()  # no ORM relationship orders runs before their session/turn parents
    # Sessions cascade to lanes/turns/messages/tool_calls/attachments(turn-linked).
    for s in db.scalars(select(ChatSession).where(ChatSession.user_id == user.id)).all():
        db.delete(s)
    for a in db.scalars(select(Attachment).where(Attachment.user_id == user.id)).all():
        db.delete(a)
    for g in db.scalars(
        select(GeneratedFile).where(GeneratedFile.user_id == user.id)
    ).all():
        db.delete(g)
    for s in db.scalars(
        select(AnswerSnapshot).where(AnswerSnapshot.user_id == user.id)
    ).all():
        db.delete(s)
    for r in db.scalars(select(EvalRun).where(EvalRun.user_id == user.id)).all():
        db.delete(r)
    for s in db.scalars(select(EvalSuite).where(EvalSuite.user_id == user.id)).all():
        db.delete(s)
    for state in db.scalars(
        select(StarterPersonaState).where(StarterPersonaState.user_id == user.id)
    ).all():
        db.delete(state)
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
    # Provider deletion also cascades to lanes in SQL. Finish ORM child deletions
    # first so loaded lanes/messages are not deleted a second time by the unit of work.
    db.flush()
    for p in db.scalars(select(Provider).where(Provider.user_id == user.id)).all():
        db.delete(p)
    db.flush()

    # passive_deletes can leave independently loaded children in the identity map
    # after SQL cascades (even when the parent's relationship was never loaded).
    # Evict only rows actually removed, not the User or another user's live objects.
    for model in (*_NESTED_TABLES.values(), DeliberationStep):
        loaded = {
            inspect(obj).identity[0]: obj
            for obj in list(db.identity_map.values()) if isinstance(obj, model)
        }
        ids = list(loaded)
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            remaining = set(db.scalars(select(model.id).where(model.id.in_(chunk))))
            for identity in set(chunk) - remaining:
                db.expunge(loaded[identity])


def restore_import(db: DbSession, user: User, zip_bytes: bytes) -> dict:
    """Validate, stage, then replace this user's graph and files as one operation.

    File replacements are atomic individually; a journal restores old bytes on Python
    exceptions, including commit failures. This is not a crash-recovery protocol across
    the filesystem and database. Serialize imports within this worker as well.
    """
    with _IMPORT_LOCK:
        journal: list[tuple[Path, Path | None]] = []
        created_dirs: list[Path] = []
        try:
            if not zip_bytes or len(zip_bytes) > MAX_BACKUP_BYTES:
                raise _invalid("empty ZIP or compressed size limit exceeded")
            try:
                zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            except (zipfile.BadZipFile, OSError, UnicodeError, NotImplementedError) as exc:
                raise _invalid("not a valid ZIP file") from exc
            with zf:
                try:
                    _check_archive(zf)
                    manifest = _read_json(zf, "manifest.json")
                    if (not isinstance(manifest, dict) or manifest.get("app") != "MultiChat"
                            or type(manifest.get("version")) is not int
                            or manifest["version"] not in (1, EXPORT_VERSION)):
                        raise _invalid("unsupported manifest or backup version")
                    data = _read_json(zf, "data.json")
                    rows = _validate_graph(data)
                    counts = manifest.get("counts")
                    if counts is not None and (not isinstance(counts, dict) or any(
                        type(count) is not int or count < 0 or
                        not isinstance(data.get(key), list) or len(data[key]) != count
                        for key, count in counts.items()
                    )):
                        raise _invalid("invalid manifest counts")
                    _validate_ownership(db, user, rows)
                    targets = _file_targets(db, user, data)
                except (zipfile.BadZipFile, EOFError, zlib.error, RuntimeError,
                        NotImplementedError) as exc:
                    raise _invalid("unreadable ZIP member") from exc
                root = Path(settings.UPLOAD_DIR).resolve()
                root.mkdir(parents=True, exist_ok=True)
                # The staging directory is on the destination filesystem (os.replace
                # must not cross volumes) and is removed even on parse/flush failures.
                with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as temporary:
                    stage = Path(temporary)
                    try:
                        staged = _stage_members(zf, stage, targets)
                    except (zipfile.BadZipFile, EOFError, zlib.error, RuntimeError,
                            NotImplementedError) as exc:
                        raise _invalid("unreadable ZIP member") from exc
                    try:
                        _restore_rows(db, user, data)
                        db.flush()  # all constraints checked before installing files
                        for index, (source, dest) in enumerate(staged):
                            _regular_path(dest)
                            if dest.parent.is_symlink() or getattr(dest.parent, "is_junction", lambda: False)():
                                raise _invalid("symbolic-link storage directory")
                            if not dest.parent.exists():
                                dest.parent.mkdir()
                                created_dirs.append(dest.parent)
                            if dest.exists():
                                old = stage / f"old-{index}"
                                shutil.copy2(dest, old)
                                os.replace(source, dest)
                                # A failed replace leaves the destination unchanged.
                                # Only journal successful replacements, so a locked
                                # path cannot block rollback of earlier writes.
                                journal.append((dest, old))
                            else:
                                # Exclusive publication: never overwrite a file created
                                # after prevalidation by another request/worker.
                                os.link(source, dest)
                                journal.append((dest, None))
                                source.unlink()
                        db.commit()
                    except BaseException:
                        # Undo files while the temporary copies are still available.
                        for dest, old in reversed(journal):
                            if old is None:
                                dest.unlink(missing_ok=True)
                            else:
                                os.replace(old, dest)
                        for directory in reversed(created_dirs):
                            directory.rmdir()
                        raise
            return {key: len(value) for key, value in data.items() if isinstance(value, list)}
        except (IntegrityError, DataError) as exc:
            db.rollback()
            raise _invalid("database constraints rejected the imported records") from exc
        except BaseException:
            db.rollback()
            raise


def _restore_rows(db: DbSession, user: User, data: dict) -> None:
    """Insert a prevalidated graph; transaction and file handling belong to the caller."""

    _delete_user_data(db, user)

    # User settings.
    u = data.get("user") or {}
    if "custom_instructions" in u:
        user.custom_instructions = u.get("custom_instructions")
    if "new_chat_use_default_persona" in u:
        user.new_chat_use_default_persona = bool(u.get("new_chat_use_default_persona"))
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
                **_timestamp_kwargs(p, "created_at"),
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
                **_timestamp_kwargs(c, "created_at"),
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
                notice=p.get("notice"),
                tools_enabled=bool(p.get("tools_enabled")),
                tool_config_json=p.get("tool_config_json"),
                is_default=bool(p.get("is_default")),
                lanes_json=p.get("lanes_json") or [],
                deliberation_json=p.get("deliberation_json"),
                **_timestamp_kwargs(p, "created_at", "updated_at"),
            )
        )

    for state in data.get("starter_persona_states", []):
        db.add(
            StarterPersonaState(
                user_id=uid,
                seed_key=state["seed_key"],
                persona_id=state.get("persona_id"),
                version=int(state.get("version", 1)),
                seeded_hash=state.get("seeded_hash"),
                dismissed=bool(state.get("dismissed")),
            )
        )

    for f in data.get("folders", []):
        db.add(
            Folder(
                id=f["id"],
                user_id=uid,
                name=f["name"],
                position=f.get("position", 0),
                **_timestamp_kwargs(f, "created_at"),
            )
        )

    for s in data.get("snippets", []):
        db.add(
            Snippet(
                id=s["id"],
                user_id=uid,
                title=s["title"],
                content=s.get("content", ""),
                **_timestamp_kwargs(s, "created_at"),
            )
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
                **_timestamp_kwargs(s, "created_at", "updated_at"),
            )
        )
    # EvalRun has a suite_id foreign key but no ORM relationship. Flush its parents
    # explicitly; otherwise SQLAlchemy may batch runs before suites on a full restore.
    db.flush()
    for r in data.get("eval_runs", []):
        db.add(
            EvalRun(
                id=r["id"],
                suite_id=r["suite_id"],
                user_id=uid,
                results_json=r.get("results_json") or [],
                summary_json=r.get("summary_json") or {},
                **_timestamp_kwargs(r, "created_at"),
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
                **_timestamp_kwargs(i, "created_at", "updated_at"),
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
                notice=s.get("notice"),
                tools_enabled=bool(s.get("tools_enabled")),
                tool_config_json=s.get("tool_config_json") or {},
                folder_id=s.get("folder_id"),
                pinned=bool(s.get("pinned")),
                archived=bool(s.get("archived")),
                trashed=bool(s.get("trashed")),
                mode=s.get("mode", "compare"),
                **_timestamp_kwargs(s, "created_at", "updated_at"),
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
                    **_timestamp_kwargs(l, "created_at"),
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
                    **_timestamp_kwargs(t, "created_at"),
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
                    ttft_ms=m.get("ttft_ms"),
                    cost_usd=m.get("cost_usd"),
                    error=m.get("error"),
                    **_timestamp_kwargs(m, "created_at"),
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
                    **_timestamp_kwargs(tc, "created_at"),
                )
            )

    # Files have already been validated/staged; only insert metadata here.
    for a in data.get("attachments", []):
        db.add(
            Attachment(
                id=a["id"],
                turn_id=a.get("turn_id"),
                user_id=uid,
                kind=a.get("kind", "image"),
                filename=a["filename"],
                mime_type=a["mime_type"],
                size_bytes=a.get("size_bytes", 0),
                storage_path=a["storage_path"],
                extracted_text=a.get("extracted_text"),
                **_timestamp_kwargs(a, "created_at"),
            )
        )
    for g in data.get("generated_files", []):
        db.add(
            GeneratedFile(
                id=g["id"],
                user_id=uid,
                session_id=g.get("session_id"),
                stored_name=g["stored_name"],
                download_name=g["download_name"],
                mime_type=g.get("mime_type", "application/octet-stream"),
                size_bytes=g.get("size_bytes", 0),
                kind=g.get("kind", "file"),
                **_timestamp_kwargs(g, "created_at"),
            )
        )
    for s in data.get("answer_snapshots", []):
        db.add(
            AnswerSnapshot(
                id=s["id"],
                user_id=uid,
                session_id=s.get("session_id"),
                prompt=s.get("prompt", ""),
                model=s["model"],
                provider_name=s.get("provider_name"),
                content=s.get("content", ""),
                label=s.get("label"),
                **_timestamp_kwargs(s, "created_at"),
            )
        )

    # Deliberation runs depend on restored sessions/turns; steps depend on runs. Their
    # transcript messages were restored above with the ordinary session data.
    db.flush()
    for r in data.get("deliberation_runs", []):
        db.add(
            DeliberationRun(
                id=r["id"],
                user_id=uid,
                session_id=r["session_id"],
                turn_id=r["turn_id"],
                status=(
                    "stopped"
                    if r.get("status") in ("pending", "running")
                    else r.get("status", "failed")
                ),
                prompt=r.get("prompt", ""),
                rounds_used=r.get("rounds_used", 0),
                converged=bool(r.get("converged")),
                config_json=r.get("config_json") or {},
                convergence_json=r.get("convergence_json") or [],
                vote_json=r.get("vote_json") or {},
                metrics_json=r.get("metrics_json") or {},
                synthesis=r.get("synthesis"),
                minority_report=r.get("minority_report"),
                extraction_json=r.get("extraction_json") or {},
                synthesis_critique_json=r.get("synthesis_critique_json") or {},
                total_calls=r.get("total_calls", 0),
                wall_ms=r.get("wall_ms", 0),
                error=r.get("error"),
                **_timestamp_kwargs(r, "created_at", "updated_at"),
            )
        )
    db.flush()
    for s in data.get("deliberation_steps", []):
        db.add(
            DeliberationStep(
                id=s["id"],
                run_id=s["run_id"],
                lane_id=s.get("lane_id"),
                message_id=s.get("message_id"),
                round_index=s.get("round_index", 0),
                phase=s.get("phase", "draft"),
                label=s.get("label"),
                model=s.get("model"),
                provider_name=s.get("provider_name"),
                verdict=s.get("verdict"),
                input_json=s.get("input_json") or {},
                output_json=s.get("output_json") or {},
                raw_text=s.get("raw_text"),
                degraded=bool(s.get("degraded")),
                error=s.get("error"),
                latency_ms=s.get("latency_ms"),
                usage_json=s.get("usage_json"),
                **_timestamp_kwargs(s, "created_at"),
            )
        )

