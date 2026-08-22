"""Query-count budgets for the endpoints that are polled or refetched constantly.

These are the tests that stop an N+1 from creeping back in: they fail on the *number* of
SQL statements, not on wall time, so they are deterministic in CI.
"""
from __future__ import annotations

import pytest

from app.perf import query_counter


def test_session_detail_query_budget(client, auth, transcript):
    """A 4-lane / 12-turn transcript must not scale its query count with its size."""
    with query_counter() as counted:
        response = client.get(f"/api/sessions/{transcript.id}", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert len(body["turns"]) == 12
    assert len(body["messages"]) == 48
    # Measured: 8. The pre-optimisation version issued one query per turn for
    # attachments alone, so a regression here shows up immediately.
    assert counted.count <= 10, f"session detail used {counted.count} queries"


def test_session_list_query_budget(client, auth, transcript):
    with query_counter() as counted:
        response = client.get("/api/sessions", headers=auth)
    assert response.status_code == 200
    assert counted.count <= 6, f"session list used {counted.count} queries"


def test_search_query_budget(client, auth, transcript):
    with query_counter() as counted:
        response = client.get("/api/sessions/search", params={"q": "prompt"}, headers=auth)
    assert response.status_code == 200
    assert response.json(), "expected the fixture transcript to match"
    # ids + rows + snippets + the auth lookup. The old version added one query per hit.
    assert counted.count <= 6, f"search used {counted.count} queries"


def test_analytics_query_budget(client, auth, transcript):
    with query_counter() as counted:
        response = client.get("/api/analytics/usage", params={"days": 7}, headers=auth)
    assert response.status_code == 200
    assert counted.count <= 8, f"analytics used {counted.count} queries"


@pytest.mark.parametrize("days", [1, 7, 0])
def test_analytics_accepts_all_ranges(client, auth, transcript, days):
    response = client.get("/api/analytics/usage", params={"days": days}, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert "kpis" in body and "daily" in body and "punchcard" in body


def test_build_lane_history_is_constant_in_turns(db, transcript):
    """History reconstruction must not issue a query per turn."""
    from app import models
    from app.broadcast import build_lane_history, load_session_turns

    lane = transcript.lanes[0]

    def count_for() -> tuple[int, int]:
        # Measure from a cold identity map, otherwise the first call gets the User for
        # free from the session cache and the second (post-commit) one re-fetches it,
        # which looks like growth but is unrelated to transcript size.
        db.expire_all()
        turns = load_session_turns(db, transcript.id)
        with query_counter() as counted:
            history = build_lane_history(db, transcript, lane, None, turns)
        return counted.count, len(history)

    before, before_len = count_for()
    assert before_len == 24  # 12 prompts + 12 answers, no system prompt in the fixture

    for order in range(12, 24):
        turn = models.Turn(session_id=transcript.id, order_index=order, content=f"p{order}")
        db.add(turn)
        db.flush()
        db.add(
            models.LaneMessage(
                lane_id=lane.id, turn_id=turn.id, role="assistant",
                content=f"a{order}", order_index=order,
            )
        )
    db.commit()

    after, after_len = count_for()
    assert after_len == 48, "the extra turns should be in the history"
    # Doubling the transcript must not change the number of statements at all.
    assert after == before, f"query count grew with transcript size: {before} -> {after}"
    assert before <= 3
