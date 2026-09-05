"""Session API regressions; all persistence uses conftest's throwaway database.

Generation is either stubbed at the stream boundary or uses an in-memory provider.
JSON exports carry transcript data, not attachment bytes or restored file references.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from urllib.parse import unquote

import pytest
from sqlalchemy import event, func, select

from app import broadcast, models
from app.db import SessionLocal, engine, get_db
from app.main import app
from app.perf import query_counter
from app.providers.base import StreamEvent
from app.routers import sessions


@pytest.fixture(autouse=True)
def no_real_providers(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Regression tests must not contact a real provider")

    monkeypatch.setattr(sessions, "build_provider", forbidden)
    monkeypatch.setattr(broadcast, "build_provider", forbidden)


@pytest.fixture
def chat(client, auth, db, user):
    provider_id = db.scalar(select(models.Provider.id).where(models.Provider.user_id == user.id))
    response = client.post(
        "/api/sessions", headers=auth,
        json={"title": "Regression", "tools_enabled": False,
              "lanes": [{"provider_id": provider_id, "model": "test-model"}]},
    )
    assert response.status_code == 200
    return response.json()


def _get(client, auth, chat, etag=None):
    headers = {**auth, **({"If-None-Match": etag} if etag else {})}
    return client.get(f"/api/sessions/{chat['id']}", headers=headers)


def _changed(client, auth, chat, etag):
    response = _get(client, auth, chat, etag)
    assert response.status_code == 200, "changed detail must not return stale 304"
    assert response.headers["etag"] != etag
    assert _get(client, auth, chat, response.headers["etag"]).status_code == 304
    return response.json()


def _answer(db, chat, *, role="responder"):
    lane = db.get(models.Lane, chat["lanes"][0]["id"])
    lane.role = role
    turn = models.Turn(session_id=chat["id"], order_index=0, content="prompt")
    db.add(turn)
    db.flush()
    message = models.LaneMessage(lane_id=lane.id, turn_id=turn.id, content="before", order_index=0)
    db.add(message)
    db.commit()
    return turn.id, message.id


@pytest.mark.parametrize("change", ["model", "hidden", "position", "provider_id", "add", "delete", "turn"])
def test_etag_tracks_lane_crud_and_unanswered_turns(client, auth, chat, db, user, change):
    path = f"/api/sessions/{chat['id']}"
    lane = chat["lanes"][0]
    if change == "turn":
        turn = models.Turn(session_id=chat["id"], content="unanswered")
        db.add(turn)
        db.commit()
        turn_id = turn.id
    if change == "provider_id":
        provider = models.Provider(user_id=user.id, name="Other", provider_type="openai_compatible")
        db.add(provider)
        db.commit()
        new_provider = provider.id
    etag = _get(client, auth, chat).headers["etag"]
    if change == "add":
        response = client.post(f"{path}/lanes", headers=auth,
                               json={"provider_id": lane["provider_id"], "model": "new"})
    elif change == "delete":
        response = client.delete(f"{path}/lanes/{lane['id']}", headers=auth)
    elif change == "turn":
        response = client.delete(f"{path}/turns/{turn_id}", headers=auth)
    else:
        value = {"model": "another", "hidden": True, "position": 3,
                 "provider_id": new_provider if change == "provider_id" else "unused"}[change]
        response = client.patch(f"{path}/lanes/{lane['id']}", headers=auth, json={change: value})
    assert response.status_code in (200, 204)
    _changed(client, auth, chat, etag)


@pytest.mark.parametrize("field,value", [
    ("title", "renamed"), ("system_prompt", "system"), ("notice", "notice"),
    ("tools_enabled", True), ("tool_config", {"web": True}),
    ("pinned", True), ("archived", True), ("trashed", True),
])
def test_session_metadata_invalidates_etag(client, auth, chat, field, value):
    etag = _get(client, auth, chat).headers["etag"]
    response = client.patch(f"/api/sessions/{chat['id']}", headers=auth, json={field: value})
    assert response.status_code == 200
    _changed(client, auth, chat, etag)


@pytest.mark.parametrize("operation", ["broadcast", "regenerate", "judge"])
def test_generation_routes_invalidate_before_any_provider_answer(client, auth, chat, db, monkeypatch, operation):
    async def empty_stream(*_args, **_kwargs):
        if False:
            yield ""

    monkeypatch.setattr(sessions, "multiplex", empty_stream)
    path = f"/api/sessions/{chat['id']}"
    if operation == "broadcast":
        endpoint, payload = f"{path}/broadcast", {"content": "new unanswered turn"}
    else:
        turn_id, _ = _answer(db, chat, role="judge" if operation == "judge" else "responder")
        endpoint = f"{path}/judge" if operation == "judge" else f"{path}/lanes/{chat['lanes'][0]['id']}/regenerate"
        payload = {"turn_id": turn_id}
    etag = _get(client, auth, chat).headers["etag"]
    assert client.post(endpoint, headers=auth, json=payload).status_code == 200
    body = _changed(client, auth, chat, etag)
    assert not body["messages"]
    assert len(body["turns"]) == 1


@pytest.mark.parametrize("outcome", ["done", "error"])
def test_actual_background_writer_invalidates_lane_state(client, auth, chat, db, monkeypatch, outcome):
    turn_id, _ = _answer(db, chat)
    before = _get(client, auth, chat).headers["etag"]
    streaming_etags = []

    class FakeProvider:
        async def stream(self, *_args):
            yield StreamEvent(type="token", text="offline answer")
            yield StreamEvent(type="done")

    async def build(*_args):
        response = _get(client, auth, chat, before)
        assert response.status_code == 200
        assert response.json()["lanes"][0]["state"] == "streaming"
        streaming_etags.append(response.headers["etag"])
        if outcome == "error":
            raise RuntimeError("offline provider failure")
        return FakeProvider()

    monkeypatch.setattr(broadcast, "build_provider", build)

    async def run():
        await broadcast.run_lane(chat["id"], chat["lanes"][0]["id"], turn_id,
                                 {"role": "user", "content": "prompt"}, asyncio.Queue())

    asyncio.run(run())
    assert streaming_etags, "must execute the real writer through the streaming state"
    body = _changed(client, auth, chat, streaming_etags[0])
    assert body["lanes"][0]["state"] == outcome


@pytest.mark.parametrize("mutation", ["message", "turn", "tool-add", "tool-edit", "tool-delete"])
def test_persisted_content_and_tool_only_writes_invalidate_etag(client, auth, chat, db, mutation):
    turn_id, message_id = _answer(db, chat)
    tool = models.ToolCall(lane_message_id=message_id, tool_name="lookup", result_json={"result": "before"})
    db.add(tool)
    db.commit()
    tool_id = tool.id
    etag = _get(client, auth, chat).headers["etag"]
    # A separate session matches the background writers, not the request identity map.
    with SessionLocal() as writer:
        if mutation == "message":
            message = writer.get(models.LaneMessage, message_id)
            assert message is not None
            message.content = "edited"  # same length
        elif mutation == "turn":
            turn = writer.get(models.Turn, turn_id)
            assert turn is not None
            turn.content = "edited"
        elif mutation == "tool-add":
            writer.add(models.ToolCall(lane_message_id=message_id, tool_name="second"))
        elif mutation == "tool-delete":
            writer.delete(writer.get(models.ToolCall, tool_id))
        else:
            saved = writer.get(models.ToolCall, tool_id)
            assert saved is not None
            saved.result_json = {"result": "edited"}
            saved.arguments_json = {"query": "new"}
            saved.citations_json = [{"title": "offline citation"}]
            saved.status = "ok"
        writer.commit()
    _changed(client, auth, chat, etag)


def test_attachment_move_invalidates_both_sessions_without_reading_bytes(client, auth, chat, db, user, monkeypatch):
    turn_id, _ = _answer(db, chat)
    other = client.post("/api/sessions", headers=auth, json={"title": "other"}).json()
    other_turn = models.Turn(session_id=other["id"], content="other")
    db.add(other_turn)
    db.flush()
    attachment = models.Attachment(user_id=user.id, turn_id=other_turn.id, kind="document",
                                   filename="metadata.txt", mime_type="text/plain", storage_path="not-written.txt")
    db.add(attachment)
    db.commit()
    etags = [_get(client, auth, c).headers["etag"] for c in (chat, other)]

    async def empty_stream(*_args):
        if False:
            yield ""

    monkeypatch.setattr(sessions, "multiplex", empty_stream)
    response = client.post(f"/api/sessions/{chat['id']}/broadcast", headers=auth,
                           json={"content": "move attachment", "attachment_ids": [attachment.id]})
    assert response.status_code == 200
    for c, etag in zip((chat, other), etags, strict=True):
        _changed(client, auth, c, etag)
    assert turn_id


def test_provider_cascade_deleting_empty_lane_invalidates_etag(client, auth, chat):
    etag = _get(client, auth, chat).headers["etag"]
    response = client.delete(f"/api/providers/{chat['lanes'][0]['provider_id']}", headers=auth)
    assert response.status_code == 204
    assert _changed(client, auth, chat, etag)["lanes"] == []


def test_etag_rollback_is_transactional_and_conditional_read_is_cheap(client, auth, transcript, db):
    chat = {"id": transcript.id}
    first = _get(client, auth, chat)
    etag = first.headers["etag"]
    message_id = first.json()["messages"][0]["id"]
    with SessionLocal() as writer:
        message = writer.get(models.LaneMessage, message_id)
        assert message is not None
        message.content = "rolled back"
        writer.flush()
        writer.rollback()
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with query_counter() as counted:
            response = _get(client, auth, chat, etag)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 304
    assert counted.count <= 5
    assert not any("lane_messages.content" in sql or "turns.content" in sql or "tool_calls.result_json" in sql
                   for sql in statements), "304 must not load or hash the transcript"


@pytest.fixture
def folders(db, user):
    foreign = models.User(email=f"foreign-{user.id}@example.test", password_hash="unused")
    db.add(foreign)
    db.flush()
    owned = models.Folder(user_id=user.id, name="owned")
    other = models.Folder(user_id=foreign.id, name="foreign")
    provider = models.Provider(user_id=foreign.id, name="foreign", provider_type="openai_compatible")
    db.add_all([owned, other, provider])
    db.commit()
    return owned.id, other.id, provider.id


@pytest.mark.parametrize("method", ["create", "update", "import"])
@pytest.mark.parametrize("folder", ["foreign", "missing"])
def test_session_rejects_unowned_folder_before_writes(client, auth, chat, db, user, folders, method, folder):
    folder_id = folders[1] if folder == "foreign" else "missing-folder"
    before = db.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id))
    payload = {"title": "must not persist", "folder_id": folder_id}
    if method == "update":
        response = client.patch(f"/api/sessions/{chat['id']}", headers=auth, json=payload)
    else:
        response = client.post("/api/sessions/import" if method == "import" else "/api/sessions",
                               headers=auth, json=payload)
    assert response.status_code == 404
    db.expire_all()
    assert db.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id)) == before
    assert db.get(models.Session, chat["id"]).title == "Regression"


def test_owned_folder_create_move_detach_and_delete(client, auth, folders):
    response = client.post("/api/sessions", headers=auth, json={"folder_id": folders[0]})
    assert response.status_code == 200
    chat = response.json()
    assert chat["folder_id"] == folders[0]
    for value in ("", folders[0]):
        etag = _get(client, auth, chat).headers["etag"]
        updated = client.patch(f"/api/sessions/{chat['id']}", headers=auth, json={"folder_id": value})
        assert updated.status_code == 200
        assert _changed(client, auth, chat, etag)["folder_id"] == (value or None)
    etag = _get(client, auth, chat).headers["etag"]
    assert client.delete(f"/api/folders/{folders[0]}", headers=auth).status_code == 204
    assert _changed(client, auth, chat, etag)["folder_id"] is None


@pytest.mark.parametrize("fmt", ["md", "json"])
@pytest.mark.parametrize("title", [
    "日本語 café 🎉", 'bad\r\nX-Injected: yes/\\"name\x00', "＂quote／path＼name", "", "a" * 1000,
])
def test_export_filename_is_safe_ascii_with_utf8_extended_name(client, auth, chat, fmt, title):
    assert client.patch(f"/api/sessions/{chat['id']}", headers=auth, json={"title": title}).status_code == 200
    response = client.get(f"/api/sessions/{chat['id']}/export", headers=auth, params={"format": fmt})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.isascii()
    assert "\r" not in disposition and "\n" not in disposition
    assert "x-injected" not in response.headers
    assert disposition.startswith('attachment; filename="')
    fallback, extended = disposition.removeprefix('attachment; filename="').split('"; filename*=UTF-8\'\'')
    decoded = unquote(extended)
    for name in (fallback, decoded):
        assert name.endswith(f".{fmt}")
        assert not any(c in name for c in '/\\"\r\n\x00')
        assert len(name) <= 160
    if title.startswith("日本語"):
        assert decoded == f"{title}.{fmt}"
    if fmt == "json":
        assert response.json()["title"] == title


def _import_payload(chat):
    return {
        "title": "imported", "lanes": [{"id": "l", "provider_id": chat["lanes"][0]["provider_id"], "model": "test"}],
        "turns": [{"id": "t", "content": "prompt", "target_lane_ids_json": ["l"]}],
        "messages": [{"lane_id": "l", "turn_id": "t", "content": "answer",
                      "tool_calls": [{"tool_name": "lookup", "result_json": {"result": "complete"}}]}],
    }


@pytest.mark.parametrize("bad", [
    "lane-id", "lane-model", "lane-shape", "lane-role", "duplicate-lane", "duplicate-turn",
    "turn-id", "turn-content", "turn-target", "message-ref", "message-cost",
    "tool-shape", "tool-arguments", "null-title", "null-lanes", "config-shape", "timestamp",
])
def test_import_validates_entire_shape_before_any_inserts(client, auth, chat, bad):
    payload = _import_payload(chat)
    if bad == "lane-id":
        del payload["lanes"][0]["id"]
    elif bad == "lane-model":
        del payload["lanes"][0]["model"]
    elif bad == "lane-shape":
        payload["lanes"] = ["not an object"]
    elif bad == "lane-role":
        payload["lanes"][0]["role"] = "not-a-role"
    elif bad == "duplicate-lane":
        payload["lanes"] *= 2
    elif bad == "duplicate-turn":
        payload["turns"] *= 2
    elif bad == "turn-id":
        del payload["turns"][0]["id"]
    elif bad == "turn-content":
        payload["turns"][0]["content"] = {"not": "text"}
    elif bad == "turn-target":
        payload["turns"][0]["target_lane_ids_json"] = "l"
    elif bad == "message-ref":
        payload["messages"][0]["turn_id"] = "outside-import"
    elif bad == "message-cost":
        payload["messages"][0]["cost_usd"] = {"bad": "type"}
    elif bad == "tool-shape":
        payload["messages"][0]["tool_calls"] = ["bad"]
    elif bad == "tool-arguments":
        payload["messages"][0]["tool_calls"][0]["arguments_json"] = []
    elif bad == "null-title":
        payload["title"] = None
    elif bad == "null-lanes":
        payload["lanes"] = None
    elif bad == "config-shape":
        payload["tool_config_json"] = []
    else:
        payload["turns"][0]["created_at"] = "invalid date"
    writes = []

    def capture(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.post("/api/sessions/import", headers=auth, json=payload)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 422
    assert not writes, "validate even late nested fields before starting persistence"


def test_import_skips_foreign_provider_lanes_and_remaps_only_local_ids(client, auth, chat, folders, db):
    payload = _import_payload(chat)
    payload["lanes"].append({"id": "foreign-lane", "provider_id": folders[2], "model": "foreign"})
    payload["lanes"].append({"id": "missing-lane", "provider_id": "missing-provider", "model": "missing"})
    payload["turns"][0]["target_lane_ids_json"] = ["l", "foreign-lane", "missing-lane"]
    payload["messages"].append({"lane_id": "foreign-lane", "turn_id": "t", "content": "skip"})
    payload["turns"][0]["attachments"] = [{"id": "foreign-file", "storage_path": "private.txt"}]
    response = client.post("/api/sessions/import", headers=auth, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert len(body["lanes"]) == len(body["messages"]) == 1
    lane_id = body["lanes"][0]["id"]
    assert lane_id not in {"l", "foreign-lane", "missing-lane"}
    targets = body["turns"][0]["target_lane_ids_json"]
    assert targets[0] == lane_id
    assert len(targets) == 3
    assert not set(targets) & {"l", "foreign-lane", "missing-lane"}
    assert not db.scalars(select(models.Lane).where(models.Lane.id.in_(targets[1:]))).all()
    assert body["messages"][0]["lane_id"] == lane_id
    assert body["messages"][0]["turn_id"] == body["turns"][0]["id"]
    assert body["turns"][0]["attachments"] == []
    assert db.scalar(select(func.count(models.Attachment.id)).join(models.Turn)
                     .where(models.Turn.session_id == body["id"])) == 0


def test_json_roundtrip_preserves_transcript_and_full_tool_results(client, auth, chat, db, folders):
    turn_id, message_id = _answer(db, chat)
    saved = db.get(models.Session, chat["id"])
    saved.notice = "important notice"
    saved.system_prompt = "system"
    saved.folder_id = folders[0]
    saved.pinned = saved.archived = True
    saved.tools_enabled = True
    saved.tool_config_json = {"options": {"nested": True}}
    lane = db.get(models.Lane, chat["lanes"][0]["id"])
    lane.hidden = True
    lane.state = "done"
    lane.position = 3
    db.get(models.Turn, turn_id).target_lane_ids_json = [lane.id]
    message = db.get(models.LaneMessage, message_id)
    message.ttft_ms, message.latency_ms = 12, 345
    message.error = "retained diagnostic"
    message.usage_json = {"completion_tokens": 123}
    message.cost_usd = 0.25
    tool = models.ToolCall(lane_message_id=message_id, tool_name="lookup", arguments_json={"q": "query"},
                           result_json={"result": "full result " * 2000, "extra": True},
                           citations_json=[{"title": "citation", "url": "https://example.test"}], status="ok")
    db.add(tool)
    db.commit()
    exported = client.get(f"/api/sessions/{chat['id']}/export", headers=auth).json()
    full = exported["messages"][0]["tool_calls"][0]
    assert full["result_json"] == tool.result_json
    assert full["result_truncated"] is False
    imported = client.post("/api/sessions/import", headers=auth, json=exported)
    assert imported.status_code == 200
    restored = client.get(f"/api/sessions/{imported.json()['id']}/export", headers=auth).json()
    original = deepcopy(exported)
    # Only generated identities (including target references) are expected to change.
    lane_map = {new["id"]: old["id"] for old, new in zip(original["lanes"], restored["lanes"], strict=True)}
    turn_map = {new["id"]: old["id"] for old, new in zip(original["turns"], restored["turns"], strict=True)}
    restored["id"] = original["id"]
    for old, new in zip(original["lanes"], restored["lanes"], strict=True):
        new["id"], new["session_id"] = old["id"], original["id"]
    for old, new in zip(original["turns"], restored["turns"], strict=True):
        new["id"], new["session_id"] = old["id"], original["id"]
        new["target_lane_ids_json"] = [lane_map[x] for x in new["target_lane_ids_json"]]
    for old, new in zip(original["messages"], restored["messages"], strict=True):
        new["id"], new["lane_id"], new["turn_id"] = old["id"], lane_map[new["lane_id"]], turn_map[new["turn_id"]]
        for old_tool, new_tool in zip(old["tool_calls"], new["tool_calls"], strict=True):
            new_tool["id"] = old_tool["id"]
    assert restored == original


def test_import_rolls_back_on_late_persistence_failure(client, auth, chat, db, user):
    before = db.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id))
    payload = _import_payload(chat)

    def fail_tool_insert(_conn, _cursor, statement, *_args):
        if statement.lower().startswith("insert into tool_calls"):
            raise RuntimeError("injected persistence failure")

    with SessionLocal() as writer:
        app.dependency_overrides[get_db] = lambda: writer
        event.listen(engine, "before_cursor_execute", fail_tool_insert)
        try:
            with pytest.raises(RuntimeError, match="injected persistence failure"):
                client.post("/api/sessions/import", headers=auth, json=payload)
            assert not writer.in_transaction(), "import must explicitly roll back a failed flush"
            assert writer.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id)) == before
        finally:
            event.remove(engine, "before_cursor_execute", fail_tool_insert)
            app.dependency_overrides.pop(get_db, None)


def test_autotitle_invalidates_detail_with_offline_provider(client, auth, chat, db, monkeypatch):
    _answer(db, chat)

    class FakeProvider:
        async def stream(self, *_args):
            yield StreamEvent(type="token", text="New Offline Title")

    async def build(*_args):
        return FakeProvider()

    monkeypatch.setattr(sessions, "build_provider", build)
    etag = _get(client, auth, chat).headers["etag"]
    response = client.post(f"/api/sessions/{chat['id']}/autotitle", headers=auth)
    assert response.status_code == 200
    assert _changed(client, auth, chat, etag)["title"] == "New Offline Title"


@pytest.mark.parametrize("operation", ["branch", "continue"])
def test_cloning_creates_independent_cache_identity(client, auth, chat, db, operation):
    turn_id, _ = _answer(db, chat)
    before = _get(client, auth, chat)
    path = f"/api/sessions/{chat['id']}"
    if operation == "branch":
        response = client.post(f"{path}/branch", headers=auth, params={"turn_id": turn_id})
    else:
        response = client.post(f"{path}/lanes/{chat['lanes'][0]['id']}/continue", headers=auth)
    assert response.status_code == 200
    clone = response.json()
    assert clone["id"] != chat["id"]
    assert len(clone["messages"]) == len(clone["turns"]) == len(clone["lanes"]) == 1
    first = _get(client, auth, clone, before.headers["etag"])
    assert first.status_code == 200
    assert _get(client, auth, clone, first.headers["etag"]).status_code == 304
    assert _get(client, auth, chat, before.headers["etag"]).status_code == 304


@pytest.mark.parametrize("operation", ["delete", "empty-trash"])
def test_deleted_session_never_revalidates_to_304(client, auth, chat, operation):
    if operation == "empty-trash":
        assert client.patch(f"/api/sessions/{chat['id']}", headers=auth, json={"trashed": True}).status_code == 200
    etag = _get(client, auth, chat).headers["etag"]
    path = "/api/sessions/trash/empty" if operation == "empty-trash" else f"/api/sessions/{chat['id']}"
    assert client.delete(path, headers=auth).status_code == 204
    assert _get(client, auth, chat, etag).status_code == 404


def test_deliberation_state_writer_invalidates_detail(client, auth, chat):
    from app.deliberation import _set_lane_states

    for state in ("streaming", "idle"):
        etag = _get(client, auth, chat).headers["etag"]
        _set_lane_states([chat["lanes"][0]["id"]], state)
        assert _changed(client, auth, chat, etag)["lanes"][0]["state"] == state


def test_imported_timestamps_do_not_disable_later_write_invalidation(client, auth, chat, db):
    payload = _import_payload(chat)
    payload["created_at"] = payload["updated_at"] = "2020-01-02T03:04:05"
    response = client.post("/api/sessions/import", headers=auth, json=payload)
    assert response.status_code == 200
    imported = response.json()
    assert imported["updated_at"] == payload["updated_at"]
    etag = _get(client, auth, imported).headers["etag"]
    db.get(models.LaneMessage, imported["messages"][0]["id"]).content = "later edit"
    db.commit()
    assert _changed(client, auth, imported, etag)["messages"][0]["content"] == "later edit"


@pytest.mark.parametrize("payload", [None, [], "not an object", 42])
def test_import_rejects_non_object_payloads(client, auth, payload):
    assert client.post("/api/sessions/import", headers=auth, json=payload).status_code == 422


@pytest.mark.parametrize("target", ["deleted", "foreign"])
def test_unavailable_targets_remain_restricted_after_import(client, auth, chat, folders, db, target):
    payload = _import_payload(chat)
    if target == "foreign":
        payload["lanes"].append({"id": "unavailable", "provider_id": folders[2], "model": "foreign"})
        payload["turns"][0]["target_lane_ids_json"] = ["unavailable"]
        payload["messages"] = []
    else:
        # A real export can contain a historical target removed by lane/provider deletion.
        deleted_lane = client.post(f"/api/sessions/{chat['id']}/lanes", headers=auth,
                                  json={"provider_id": chat["lanes"][0]["provider_id"], "model": "deleted"}).json()
        turn_id, _ = _answer(db, chat)
        db.get(models.Turn, turn_id).target_lane_ids_json = [deleted_lane["id"]]
        db.commit()
        assert client.delete(f"/api/sessions/{chat['id']}/lanes/{deleted_lane['id']}", headers=auth).status_code == 204
        payload = client.get(f"/api/sessions/{chat['id']}/export", headers=auth).json()
    old_target = payload["turns"][0]["target_lane_ids_json"][0]
    response = client.post("/api/sessions/import", headers=auth, json=payload)
    assert response.status_code == 200
    imported = response.json()
    targets = imported["turns"][0]["target_lane_ids_json"]
    assert targets and old_target not in targets, "retain restriction without cross-import ids"
    saved = db.get(models.Session, imported["id"])
    lane = db.get(models.Lane, imported["lanes"][0]["id"])
    assert saved is not None and lane is not None
    assert broadcast.build_lane_history(db, saved, lane) == []


@pytest.mark.parametrize("invalid_json", [r'{"result":"\ud800"}', '{"result":NaN}'])
def test_import_rejects_unserializable_nested_json_before_commit(client, auth, chat, db, user, invalid_json):
    import json

    before = db.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id))
    payload = _import_payload(chat)
    payload["messages"][0]["tool_calls"][0]["result_json"] = json.loads(invalid_json)
    response = client.post("/api/sessions/import", headers={**auth, "Content-Type": "application/json"},
                           content=json.dumps(payload))
    assert response.status_code == 422
    assert db.scalar(select(func.count(models.Session.id)).where(models.Session.user_id == user.id)) == before

