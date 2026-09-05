from __future__ import annotations

import asyncio
import copy
import io
import json
import struct
import zipfile
from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app import models, system_backup
from app.config import settings
from app.routers import system as system_router
from app.system_backup import build_export, restore_import


def archive(data, files=None, manifest=None, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr("manifest.json", json.dumps(
            manifest if manifest is not None else {"version": 2, "app": "MultiChat"}
        ))
        zf.writestr("data.json", json.dumps(data))
        for name, content in (files or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def graph(user):
    data = {
        "user": {"custom_instructions": "restored", "new_chat_use_default_persona": False},
        "providers": [{"id": "p", "name": "provider", "provider_type": "openai_compatible"}],
        "personas": [{"id": "persona", "name": "persona", "lanes_json": [
            {"provider_id": "p", "model": "model"}]}],
        "starter_persona_states": [{"seed_key": "starter", "persona_id": "persona"}],
        "folders": [{"id": "folder", "name": "folder"}],
        "eval_suites": [{"id": "suite", "name": "suite", "models_json": [
            {"provider_id": "p", "model": "model"}]}],
        "eval_runs": [{"id": "eval", "suite_id": "suite"}],
        "sessions": [{
            "id": "s", "folder_id": "folder",
            "lanes": [{"id": "l", "provider_id": "p", "model": "model"}],
            "turns": [{"id": "t", "target_lane_ids_json": ["l"]}],
            "messages": [{"id": "m", "lane_id": "l", "turn_id": "t"}],
            "tool_calls": [{"id": "tc", "lane_message_id": "m", "tool_name": "search"}],
        }],
        "attachments": [{"id": "a", "turn_id": "t", "filename": "a.txt",
                         "mime_type": "text/plain", "storage_path": "a.txt"}],
        "generated_files": [{"id": "g", "session_id": "s", "stored_name": "g.txt",
                             "download_name": "g.txt"}],
        "answer_snapshots": [{"id": "snap", "session_id": "s", "model": "model"}],
        "deliberation_runs": [{"id": "run", "session_id": "s", "turn_id": "t"}],
        "deliberation_steps": [{"id": "step", "run_id": "run", "lane_id": "l",
                                "message_id": "m"}],
    }
    # conftest shares one temporary database across tests: use distinct ids/names even
    # when a regression makes a malicious import unexpectedly commit.
    ids = {"p", "persona", "folder", "suite", "eval", "s", "l", "t", "m", "tc",
           "a", "g", "snap", "run", "step", "a.txt", "g.txt"}

    def unique(value):
        if isinstance(value, dict):
            return {key: unique(item) for key, item in value.items()}
        if isinstance(value, list):
            return [unique(item) for item in value]
        return f"{user.id}-{value}" if isinstance(value, str) and value in ids else value

    return unique(data)


def set_path(data, path, value):
    for part in path[:-1]:
        data = data[part]
    data[path[-1]] = value


REFERENCE_PATHS = [
    ("sessions", 0, "lanes", 0, "provider_id"),
    ("sessions", 0, "folder_id"),
    ("sessions", 0, "messages", 0, "lane_id"),
    ("sessions", 0, "messages", 0, "turn_id"),
    ("sessions", 0, "tool_calls", 0, "lane_message_id"),
    ("sessions", 0, "turns", 0, "target_lane_ids_json", 0),
    ("attachments", 0, "turn_id"),
    ("generated_files", 0, "session_id"),
    ("answer_snapshots", 0, "session_id"),
    ("eval_runs", 0, "suite_id"),
    ("starter_persona_states", 0, "persona_id"),
    ("personas", 0, "lanes_json", 0, "provider_id"),
    ("eval_suites", 0, "models_json", 0, "provider_id"),
    ("deliberation_runs", 0, "session_id"),
    ("deliberation_runs", 0, "turn_id"),
    ("deliberation_steps", 0, "run_id"),
    ("deliberation_steps", 0, "lane_id"),
    ("deliberation_steps", 0, "message_id"),
]


@pytest.mark.parametrize("path", REFERENCE_PATHS)
def test_references_must_be_closed_before_delete(db, user, graph, monkeypatch, path):
    set_path(graph, path, "not-imported")
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


def test_foreign_provider_reference_is_rejected(db, user, graph, monkeypatch):
    other = models.User(email=f"other-{user.id}@example.test", password_hash="unused")
    db.add(other)
    db.flush()
    provider = models.Provider(user_id=other.id, name="private", provider_type="openai_compatible")
    db.add(provider)
    db.commit()
    graph["sessions"][0]["lanes"][0]["provider_id"] = provider.id
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


@pytest.mark.parametrize("field", ["lane_id", "turn_id"])
def test_message_cannot_cross_imported_sessions(db, user, graph, monkeypatch, field):
    lane_id, turn_id = f"{user.id}-l2", f"{user.id}-t2"
    second = {"id": f"{user.id}-s2", "lanes": [{"id": lane_id,
              "provider_id": graph["providers"][0]["id"], "model": "model"}],
              "turns": [{"id": turn_id}]}
    graph["sessions"].append(second)
    graph["sessions"][0]["messages"][0][field] = lane_id if field == "lane_id" else turn_id
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


@pytest.mark.parametrize("mutation", [
    (("providers",), {}), (("sessions", 0, "lanes"), None),
    (("providers", 0, "name"), []), (("providers", 0, "id"), 42),
    (("providers", 0, "api_key"), {"secret": "bad"}),
    (("sessions", 0, "pinned"), "false"),
    (("attachments", 0, "size_bytes"), 2**100),
    (("deliberation_steps", 0, "usage_json"), []),
])
def test_invalid_field_types_fail_before_delete(db, user, graph, monkeypatch, mutation):
    path, value = mutation
    set_path(graph, path, value)
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


@pytest.mark.parametrize("data", [None, [], {}, {"user": []}, {"user": {}, "providers": [None]}])
def test_invalid_document_returns_400(client, auth, data):
    response = client.post("/api/system/import", headers=auth,
                           files={"file": ("backup.zip", archive(data), "application/zip")})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("manifest", [[], {"version": 999, "app": "MultiChat"},
                                     {"version": True, "app": "MultiChat"},
                                     {"version": 2, "app": "other"}])
def test_invalid_manifest_returns_400(client, auth, manifest):
    response = client.post("/api/system/import", headers=auth, files={
        "file": ("backup.zip", archive({"user": {}}, manifest=manifest), "application/zip")})
    assert response.status_code == 400, response.text


def test_file_rollback_on_commit_failure(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    original = tmp_path / "original.txt"
    original.write_bytes(b"original bytes")
    attachment = models.Attachment(user_id=user.id, filename="original.txt", mime_type="text/plain",
                                   storage_path="original.txt")
    db.add(attachment)
    db.commit()
    attachment_id = attachment.id
    data = {"user": {}, "attachments": [{"id": attachment_id, "filename": "original.txt",
            "mime_type": "text/plain", "storage_path": "original.txt"}]}
    monkeypatch.setattr(db, "commit", Mock(side_effect=RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        restore_import(db, user, archive(data, {"uploads/original.txt": b"replacement"}))
    assert original.read_bytes() == b"original bytes"
    assert set(p.name for p in tmp_path.iterdir()) == {"original.txt"}
    assert db.get(models.Attachment, attachment_id) is not None


@pytest.mark.parametrize("include_bytes", [True, False])
def test_foreign_file_name_is_rejected_even_without_member(db, user, tmp_path, monkeypatch, include_bytes):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    other = models.User(email=f"files-{user.id}@example.test", password_hash="unused")
    db.add(other)
    db.flush()
    db.add(models.Attachment(user_id=other.id, filename="private", mime_type="text/plain",
                             storage_path="private.txt"))
    db.commit()
    (tmp_path / "private.txt").write_bytes(b"private bytes")
    data = {"user": {}, "attachments": [{"id": f"new-{user.id}", "filename": "stolen",
            "mime_type": "text/plain", "storage_path": "private.txt"}]}
    files = {"uploads/private.txt": b"overwritten"} if include_bytes else {}
    with pytest.raises(ValueError):
        restore_import(db, user, archive(data, files))
    assert (tmp_path / "private.txt").read_bytes() == b"private bytes"


@pytest.mark.parametrize("name", [".jwt_secret", "../outside", "x\\y", "C:secret", "NUL.txt",
                                  "name.", "name ", "bad\x00name", "generated", "COM1.log",
                                  "LPT¹.txt", "x" * 256])
def test_unsafe_storage_names_reject_entire_backup(db, user, tmp_path, monkeypatch, name):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    data = {"user": {}, "attachments": [{"id": f"a-{user.id}", "filename": "x", "mime_type": "text/plain",
                                         "storage_path": name}]}
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(data))
    delete.assert_not_called()


def test_same_user_file_roundtrip(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    (tmp_path / "roundtrip.txt").write_bytes(b"saved")
    db.add(models.Attachment(user_id=user.id, filename="friendly.txt", mime_type="text/plain",
                             storage_path="roundtrip.txt", extracted_text="text"))
    db.commit()
    blob = build_export(db, user)
    (tmp_path / "roundtrip.txt").write_bytes(b"changed")
    restore_import(db, user, blob)
    assert (tmp_path / "roundtrip.txt").read_bytes() == b"saved"
    assert db.scalar(select(models.Attachment).where(models.Attachment.user_id == user.id)).extracted_text == "text"


def test_duplicate_ids_rejected_before_delete(db, user, graph, monkeypatch):
    graph["providers"].append(copy.deepcopy(graph["providers"][0]))
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


def test_full_graph_and_secrets_roundtrip_twice(db, user, graph, tmp_path, monkeypatch):
    from app.crypto import decrypt

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    graph["providers"][0]["api_key"] = "test-only-key"
    graph["providers"][0]["oauth_refresh_token"] = "test-only-refresh"
    graph["deliberation_runs"][0]["status"] = "running"
    graph["starter_persona_states"].append({"seed_key": "dismissed", "persona_id": None, "dismissed": True})
    graph["personas"][0]["lanes_json"].append({"provider_id": "", "model": "unconfigured"})
    filename = graph["attachments"][0]["storage_path"]
    generated = graph["generated_files"][0]["stored_name"]
    restore_import(db, user, archive(graph, {f"uploads/{filename}": b"attachment",
                                           f"generated/{generated}": b"generated"}))
    for _ in range(2):
        # Include loaded ORM relationships in the same session, not only fresh sessions.
        chat = db.get(models.Session, graph["sessions"][0]["id"])
        assert chat.lanes[0].messages[0].tool_calls
        blob = build_export(db, user)
        summary = restore_import(db, user, blob)
        assert summary["deliberation_steps"] == 1
        provider = db.get(models.Provider, graph["providers"][0]["id"])
        assert decrypt(provider.api_key_encrypted) == "test-only-key"
        assert decrypt(provider.oauth_refresh_token_encrypted) == "test-only-refresh"
        assert db.get(models.DeliberationRun, graph["deliberation_runs"][0]["id"]).status == "stopped"
        assert (tmp_path / filename).read_bytes() == b"attachment"
        assert (tmp_path / "generated" / generated).read_bytes() == b"generated"
        assert not list(tmp_path.glob(".restore-*"))


ALL_ID_TABLES = [key for key in {**system_backup._TABLES, **system_backup._NESTED_TABLES}
                 if key != "starter_persona_states"]


@pytest.mark.parametrize("table", ALL_ID_TABLES)
def test_foreign_ids_in_every_table_rejected_before_delete(db, user, graph, monkeypatch, table):
    graph["tool_credentials"] = [{"id": f"{user.id}-cred", "tool": "search"}]
    graph["snippets"] = [{"id": f"{user.id}-snippet", "title": "snippet"}]
    graph["integrations"] = [{"id": f"{user.id}-integration", "kind": "workiq", "enabled": False}]
    other = models.User(email=f"ids-{user.id}@example.test", password_hash="unused")
    db.add(other)
    db.commit()
    restore_import(db, other, archive(graph))
    rows = system_backup._validate_graph(graph)
    collision = next(iter(rows[table]))
    replacements = {identity: f"new-{identity}" for items in rows.values() for identity in items
                    if identity != collision}

    def remap(value):
        if isinstance(value, dict):
            return {key: remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    incoming = remap(graph)
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError, match="record id belongs"):
        restore_import(db, user, archive(incoming))
    delete.assert_not_called()


@pytest.mark.parametrize("case_alias", [False, True])
def test_generated_file_tenant_collision(db, user, tmp_path, monkeypatch, case_alias):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    other = models.User(email=f"gen-{user.id}@example.test", password_hash="unused")
    db.add(other)
    db.flush()
    name = f"{user.id}-Private.txt"
    db.add(models.GeneratedFile(user_id=other.id, stored_name=name, download_name="private.txt"))
    db.commit()
    (tmp_path / "generated").mkdir()
    original = tmp_path / "generated" / name
    original.write_bytes(b"private")
    incoming = name.lower() if case_alias else name
    data = {"user": {}, "generated_files": [{"id": f"new-{user.id}", "stored_name": incoming,
                                             "download_name": "stolen"}]}
    with pytest.raises(ValueError, match="another user"):
        restore_import(db, user, archive(data, {f"generated/{incoming}": b"overwrite"}))
    assert original.read_bytes() == b"private"


def test_shared_attachment_and_legacy_duplicate_members(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    name = f"{user.id}-shared.txt"
    data = {"user": {}, "attachments": [{"id": f"{user.id}-{i}", "filename": "shared.txt",
            "mime_type": "text/plain", "storage_path": name} for i in range(2)]}
    buf = io.BytesIO(archive(data, {f"uploads/{name}": b"shared"}))
    # Older build_export wrote one ZIP entry per attachment row, including clones.
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf, pytest.warns(UserWarning):
        zf.writestr(f"uploads/{name}", b"shared")
    restore_import(db, user, buf.getvalue())
    exported = build_export(db, user)
    with zipfile.ZipFile(io.BytesIO(exported)) as zf:
        assert zf.namelist().count(f"uploads/{name}") == 1
    restore_import(db, user, exported)
    assert (tmp_path / name).read_bytes() == b"shared"
    assert len(db.scalars(select(models.Attachment).where(models.Attachment.user_id == user.id)).all()) == 2


def test_export_detaches_deleted_soft_links_without_mutating_rows(db, user, graph):
    restore_import(db, user, archive(graph))
    snapshot = db.get(models.AnswerSnapshot, graph["answer_snapshots"][0]["id"])
    snapshot.session_id = "deleted-session"
    step = db.get(models.DeliberationStep, graph["deliberation_steps"][0]["id"])
    step.lane_id, step.message_id = "deleted-lane", "deleted-message"
    persona = db.get(models.Persona, graph["personas"][0]["id"])
    persona.lanes_json = [{"provider_id": "deleted-provider", "model": "keep-hint"}]
    db.commit()
    exported = build_export(db, user)
    assert snapshot.session_id == "deleted-session"
    assert step.lane_id == "deleted-lane"
    assert persona.lanes_json[0]["provider_id"] == "deleted-provider"
    restore_import(db, user, exported)
    assert db.get(models.AnswerSnapshot, snapshot.id).session_id is None
    assert db.get(models.DeliberationStep, step.id).message_id is None
    assert db.get(models.Persona, persona.id).lanes_json == [{"provider_id": "", "model": "keep-hint"}]


@pytest.mark.parametrize("field,value", [("turn_id", "second-turn"), ("lane_id", "second-lane")])
def test_deliberation_message_matches_its_run_and_lane(db, user, graph, monkeypatch, field, value):
    session = graph["sessions"][0]
    session["turns"].append({"id": "second-turn"})
    session["lanes"].append({"id": "second-lane", "model": "model", "provider_id": graph["providers"][0]["id"]})
    session["messages"][0][field] = value
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError, match="deliberation message"):
        restore_import(db, user, archive(graph))
    delete.assert_not_called()


@pytest.mark.parametrize("kind", ["corrupt-file", "corrupt-unreferenced", "duplicate-json", "duplicate-file",
                                   "truncated", "missing-data", "missing-manifest", "bad-json", "bad-utf8",
                                   "json-duplicate-key", "infinite-number", "surrogate", "deep-json",
                                   "unsafe-member", "encrypted-member", "unsupported-compression"])
def test_bad_archives_400_without_destructive_work(client, auth, db, user, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    name = f"{user.id}-old.txt"
    original = tmp_path / name
    original.write_bytes(b"original")
    attachment = models.Attachment(user_id=user.id, storage_path=name, filename="old", mime_type="text/plain")
    db.add(attachment)
    db.commit()
    data = {"user": {}, "attachments": [{"id": attachment.id, "storage_path": name,
                                         "filename": "old", "mime_type": "text/plain"}]}
    members = [("manifest.json", b'{"version":2,"app":"MultiChat"}'),
               ("data.json", json.dumps(data).encode()), (f"uploads/{name}", b"replacement")]
    if kind == "missing-data":
        members.pop(1)
    elif kind == "missing-manifest":
        members.pop(0)
    elif kind in {"bad-json", "bad-utf8", "json-duplicate-key", "infinite-number", "surrogate", "deep-json"}:
        payload = {"bad-json": b"{", "bad-utf8": b"\xff", "json-duplicate-key": b'{"user":{},"user":{}}',
                   "infinite-number": b'{"user":{},"extra":1e999}',
                   "surrogate": b'{"user":{"custom_instructions":"\\ud800"}}',
                   "deep-json": b"[" * 1500 + b"]" * 1500}[kind]
        members[1] = ("data.json", payload)
    elif kind == "unsafe-member":
        members.append(("uploads/../.jwt_secret", b"secret"))
    elif kind == "duplicate-json":
        members.append(("data.json", json.dumps(data).encode()))
    elif kind == "duplicate-file":
        members.append((f"uploads/{name}", b"different"))
    elif kind == "corrupt-unreferenced":
        members.append(("uploads/unreferenced.txt", b"bad-crc"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for path, content in members:
            if path in zf.namelist():
                with pytest.warns(UserWarning):
                    zf.writestr(path, content)
            else:
                zf.writestr(path, content)
    blob = bytearray(buf.getvalue())
    if kind.startswith("corrupt-"):
        marker = b"bad-crc" if kind == "corrupt-unreferenced" else b"replacement"
        blob[blob.index(marker)] ^= 1
    elif kind == "truncated":
        blob = blob[:-30]
    elif kind in {"encrypted-member", "unsupported-compression"}:
        central = blob.index(b"PK\x01\x02")
        if kind == "encrypted-member":
            struct.pack_into("<H", blob, central + 8, 1)
        else:
            struct.pack_into("<H", blob, central + 10, 99)
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    response = client.post("/api/system/import", headers=auth,
                           files={"file": ("backup.zip", bytes(blob), "application/zip")})
    assert response.status_code == 400, response.text
    delete.assert_not_called()
    assert original.read_bytes() == b"original"
    assert not list(tmp_path.glob(".restore-*"))
    db.expire_all()
    assert db.get(models.Attachment, attachment.id) is not None


@pytest.mark.parametrize("limit", ["MAX_BACKUP_BYTES", "MAX_JSON_BYTES", "MAX_EXPANDED_BYTES", "MAX_BACKUP_MEMBERS"])
def test_archive_limits_checked_before_deletion(client, auth, monkeypatch, limit):
    blob = archive({"user": {"custom_instructions": "x" * 8192}})
    monkeypatch.setattr(system_backup, limit, 1 if limit == "MAX_BACKUP_MEMBERS" else 128)
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    response = client.post("/api/system/import", headers=auth,
                           files={"file": ("backup.zip", blob, "application/zip")})
    assert response.status_code == 400, response.text
    delete.assert_not_called()


def test_high_compression_text_and_v1_missing_optional_collections(db, user):
    text = "x" * (2 * 1024 * 1024)
    data = {"user": {"custom_instructions": text}}
    blob = archive(data, manifest={"version": 1, "app": "MultiChat"})
    assert len(blob) < len(text) // 100
    restore_import(db, user, blob)
    assert user.custom_instructions == text


def test_mid_publication_failure_restores_old_and_removes_new_files(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    old = tmp_path / "old.txt"
    old.write_bytes(b"original")
    db.add(models.Attachment(user_id=user.id, storage_path="old.txt", filename="old", mime_type="text/plain"))
    db.commit()
    data = {"user": {}, "attachments": [{"id": f"{user.id}-{i}", "storage_path": name,
            "filename": name, "mime_type": "text/plain"}
            for i, name in enumerate(["old.txt", "new.txt", "failure.txt"])]}
    real_link = system_backup.os.link

    def fail_last(source, dest):
        if dest.name == "failure.txt":
            raise OSError("simulated publication failure")
        return real_link(source, dest)

    monkeypatch.setattr(system_backup.os, "link", fail_last)
    with pytest.raises(OSError, match="simulated"):
        restore_import(db, user, archive(data, {f"uploads/{name}": b"new"
                       for name in ["old.txt", "new.txt", "failure.txt"]}))
    assert old.read_bytes() == b"original"
    assert {p.name for p in tmp_path.iterdir()} == {"old.txt"}
    assert len(db.scalars(select(models.Attachment).where(models.Attachment.user_id == user.id)).all()) == 1


def test_late_constraint_failure_rolls_back_db_before_files_are_published(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    user.custom_instructions = "original settings"
    db.commit()
    real_restore = system_backup._restore_rows

    def fail_constraint(db, user, data):
        real_restore(db, user, data)
        db.add(models.Provider(user_id=user.id, name="invalid", provider_type=None))

    monkeypatch.setattr(system_backup, "_restore_rows", fail_constraint)
    with pytest.raises(ValueError, match="database constraints"):
        restore_import(db, user, archive({"user": {"custom_instructions": "new"},
                       "generated_files": [{"id": user.id, "stored_name": "new.txt", "download_name": "new"}]},
                       {"generated/new.txt": b"new"}))
    assert user.custom_instructions == "original settings"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("existing", ["orphan", "directory", "symlink"])
def test_non_owned_or_non_regular_destinations_rejected(db, user, tmp_path, monkeypatch, existing):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    target = tmp_path / "reserved.txt"
    if existing == "directory":
        target.mkdir()
    elif existing == "symlink":
        secret = tmp_path / ".jwt_secret"
        secret.write_bytes(b"secret")
        try:
            target.symlink_to(secret)
        except OSError:
            # Windows may require Developer Mode for symlinks; exercise the same
            # prevalidation branch without privileges or touching a real secret.
            real_check = system_backup.Path.is_symlink
            monkeypatch.setattr(system_backup.Path, "is_symlink",
                                lambda path: path == target or real_check(path))
    else:
        target.write_bytes(b"orphan")
    data = {"user": {}, "attachments": [{"id": user.id, "storage_path": "reserved.txt",
                                          "filename": "reserved", "mime_type": "text/plain"}]}
    with pytest.raises(ValueError):
        restore_import(db, user, archive(data, {"uploads/reserved.txt": b"replacement"}))
    if existing == "symlink":
        assert (tmp_path / ".jwt_secret").read_bytes() == b"secret"


def test_export_will_not_read_reserved_secret(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    secret = tmp_path / ".jwt_secret"
    secret.write_bytes(b"test secret")
    db.add(models.Attachment(user_id=user.id, storage_path=".jwt_secret", filename="x", mime_type="text/plain"))
    db.commit()
    with pytest.raises(ValueError, match="unsafe storage"):
        build_export(db, user)


def test_failed_replace_does_not_block_rollback_of_earlier_files(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    for name in ("first.txt", "blocked.txt"):
        (tmp_path / name).write_bytes(b"original")
        db.add(models.Attachment(user_id=user.id, storage_path=name, filename=name, mime_type="text/plain"))
    db.commit()
    data = {"user": {}, "attachments": [{"id": f"{user.id}-{name}", "storage_path": name,
            "filename": name, "mime_type": "text/plain"} for name in ("first.txt", "blocked.txt")]}
    real_replace = system_backup.os.replace

    def blocked_destination(source, dest):
        if dest.name == "blocked.txt":
            raise PermissionError("destination locked")
        return real_replace(source, dest)

    monkeypatch.setattr(system_backup.os, "replace", blocked_destination)
    with pytest.raises(PermissionError, match="locked"):
        restore_import(db, user, archive(data, {"uploads/first.txt": b"new", "uploads/blocked.txt": b"new"}))
    assert (tmp_path / "first.txt").read_bytes() == b"original"
    assert (tmp_path / "blocked.txt").read_bytes() == b"original"
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_non_export_compression_methods_rejected(db, user, monkeypatch, compression):
    blob = archive({"user": {}}, compression=compression)
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    with pytest.raises(ValueError, match="unsupported ZIP compression"):
        restore_import(db, user, blob)
    delete.assert_not_called()


@pytest.mark.parametrize("counts", [[], {"providers": -1}, {"providers": True}, {"providers": 1}])
def test_manifest_counts_are_validated(db, user, counts):
    with pytest.raises(ValueError, match="manifest counts"):
        restore_import(db, user, archive({"user": {}, "providers": []},
                       manifest={"version": 2, "app": "MultiChat", "counts": counts}))


def test_composite_and_nested_duplicate_ids_rejected(db, user, graph, monkeypatch):
    delete = Mock(wraps=system_backup._delete_user_data)
    monkeypatch.setattr(system_backup, "_delete_user_data", delete)
    duplicate = copy.deepcopy(graph)
    duplicate["starter_persona_states"].append(copy.deepcopy(duplicate["starter_persona_states"][0]))
    with pytest.raises(ValueError, match="duplicate id"):
        restore_import(db, user, archive(duplicate))
    duplicate = copy.deepcopy(graph)
    duplicate["sessions"].append({"id": f"second-{user.id}", "lanes": duplicate["sessions"][0]["lanes"]})
    with pytest.raises(ValueError, match="duplicate id"):
        restore_import(db, user, archive(duplicate))
    delete.assert_not_called()


@pytest.mark.parametrize("empty", [True, False])
def test_route_bounds_read_and_closes_upload(db, user, monkeypatch, empty):
    class TrackedFile(io.BytesIO):
        def read(self, size=-1):
            assert size == 129
            return super().read(size)

    monkeypatch.setattr(system_backup, "MAX_BACKUP_BYTES", 128)
    backing = TrackedFile(b"" if empty else b"x" * 500)
    upload = UploadFile(file=backing, filename="backup.zip")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(system_router.import_everything(upload, user, db))
    assert caught.value.status_code == 400
    assert backing.closed


def test_unexpected_server_errors_do_not_expose_import_secrets(client, auth, monkeypatch):
    monkeypatch.setattr(system_router, "restore_import", Mock(side_effect=RuntimeError("private SQL/token detail")))
    response = client.post("/api/system/import", headers=auth,
                           files={"file": ("backup.zip", archive({"user": {}}), "application/zip")})
    assert response.status_code == 500
    assert response.json() == {"detail": "Import failed"}


@pytest.mark.parametrize("failure", [False, True])
def test_archive_and_staging_handles_closed(db, user, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    name = f"{user.id}-file.txt"
    blob = archive({"user": {}, "generated_files": [{"id": user.id, "stored_name": name,
                   "download_name": "file.txt"}]}, {f"generated/{name}": b"bytes"})
    opened = []
    original_zip = zipfile.ZipFile

    class TrackedZip(original_zip):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

    monkeypatch.setattr(system_backup.zipfile, "ZipFile", TrackedZip)
    if failure:
        monkeypatch.setattr(db, "commit", Mock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError):
            restore_import(db, user, blob)
        assert not list(tmp_path.iterdir())
    else:
        restore_import(db, user, blob)
        assert not list(tmp_path.glob(".restore-*"))
    assert opened and all(zf.fp is None for zf in opened)


def test_generated_directory_symlink_rejected(db, user, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(root))
    link = root / "generated"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        real_check = system_backup.Path.is_symlink
        monkeypatch.setattr(system_backup.Path, "is_symlink", lambda path: path == link or real_check(path))
    data = {"user": {}, "generated_files": [{"id": user.id, "stored_name": "file.txt", "download_name": "x"}]}
    with pytest.raises(ValueError, match="symbolic-link storage directory"):
        restore_import(db, user, archive(data, {"generated/file.txt": b"replacement"}))
    assert not list(outside.iterdir())


def test_lane_and_tool_call_creation_times_survive_roundtrip(db, user, transcript):
    historical = datetime(2001, 2, 3, 4, 5, 6)
    lane = transcript.lanes[0]
    call = lane.messages[0].tool_calls[0]
    lane.created_at = call.created_at = historical
    db.commit()
    lane_id, call_id = lane.id, call.id
    restore_import(db, user, build_export(db, user))
    assert db.get(models.Lane, lane_id).created_at == historical
    assert db.get(models.ToolCall, call_id).created_at == historical



