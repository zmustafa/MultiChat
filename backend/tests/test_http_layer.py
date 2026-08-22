"""End-to-end behaviour of the optimised HTTP layer: compression, ETags and payload
trimming. These guard changes that are invisible to a type checker."""
from __future__ import annotations

from app.config import settings


def test_transcript_is_gzipped(client, auth, transcript):
    response = client.get(
        f"/api/sessions/{transcript.id}",
        headers={**auth, "Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    # httpx decodes transparently, so assert on the header and on a real round trip.
    assert response.headers.get("content-encoding") == "gzip"
    assert response.headers.get("vary", "").lower().find("accept-encoding") >= 0
    assert len(response.json()["messages"]) == 48


def test_small_response_is_not_gzipped(client, auth):
    response = client.get(
        "/api/sessions/active", headers={**auth, "Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_streaming_response_is_never_gzipped(client, auth, transcript):
    """SSE must pass through uncompressed or live streaming stops being live."""
    with client.stream(
        "POST",
        f"/api/sessions/{transcript.id}/resume",
        headers={**auth, "Accept-Encoding": "gzip"},
        json={"turn_id": transcript.turns[0].id},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in response.headers


def test_etag_revalidation_returns_304(client, auth, transcript):
    first = client.get(f"/api/sessions/{transcript.id}", headers=auth)
    etag = first.headers.get("etag")
    assert etag, "session detail should carry an ETag"

    second = client.get(
        f"/api/sessions/{transcript.id}", headers={**auth, "If-None-Match": etag}
    )
    assert second.status_code == 304
    assert second.content == b""


def test_etag_changes_when_a_message_is_added(client, auth, transcript, db):
    from app import models

    etag = client.get(f"/api/sessions/{transcript.id}", headers=auth).headers["etag"]
    lane = transcript.lanes[0]
    turn = transcript.turns[0]
    db.add(
        models.LaneMessage(
            lane_id=lane.id, turn_id=turn.id, role="assistant", content="new", order_index=99
        )
    )
    db.commit()

    refreshed = client.get(
        f"/api/sessions/{transcript.id}", headers={**auth, "If-None-Match": etag}
    )
    assert refreshed.status_code == 200


def test_tool_results_are_truncated_with_a_full_fetch_available(client, auth, transcript):
    body = client.get(f"/api/sessions/{transcript.id}", headers=auth).json()
    calls = [tc for m in body["messages"] for tc in m["tool_calls"]]
    assert calls, "fixture should have tool calls"
    call = calls[0]
    assert call["result_truncated"] is True
    assert len(call["result_json"]["result"]) == settings.TOOL_RESULT_PREVIEW_CHARS

    full = client.get(
        f"/api/sessions/{transcript.id}/tool-calls/{call['id']}", headers=auth
    )
    assert full.status_code == 200
    assert len(full.json()["result_json"]["result"]) == 9000


def test_tool_call_from_another_session_is_not_readable(client, auth, transcript, db, user):
    from app import models

    other = models.Session(user_id=user.id, title="other")
    db.add(other)
    db.commit()

    body = client.get(f"/api/sessions/{transcript.id}", headers=auth).json()
    call_id = next(tc["id"] for m in body["messages"] for tc in m["tool_calls"])

    response = client.get(f"/api/sessions/{other.id}/tool-calls/{call_id}", headers=auth)
    assert response.status_code == 404


def test_transcript_requires_authentication(client, transcript):
    assert client.get(f"/api/sessions/{transcript.id}").status_code == 401


def test_transcript_of_another_user_is_404(client, transcript, db):
    from app import models
    from app.security import create_access_token

    intruder = models.User(email="intruder@example.test", password_hash="x")
    db.add(intruder)
    db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(intruder.id)}"}
    assert client.get(f"/api/sessions/{transcript.id}", headers=headers).status_code == 404
