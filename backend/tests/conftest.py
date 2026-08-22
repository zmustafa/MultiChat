"""Shared fixtures.

Every test runs against a throwaway SQLite file and upload directory. The environment is
configured *before* ``app`` is imported, because the engine, the Fernet key and the JWT
secret are all resolved at import time.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="multichat-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["APP_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    hashlib.sha256(b"multichat-test-key").digest()
).decode()
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256!!"
os.environ["PERF_LOG"] = "1"  # installs the SQL statement counter
(_TMP / "uploads").mkdir(parents=True, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import models  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user(client, db) -> models.User:
    """A throwaway user with one provider, created fresh per test."""
    u = models.User(email=f"t-{os.urandom(4).hex()}@example.test", password_hash=hash_password("pw"))
    db.add(u)
    db.flush()
    db.add(
        models.Provider(
            user_id=u.id,
            name="Test provider",
            provider_type="openai_compatible",
            default_model="test-model",
            models_json=["test-model"],
        )
    )
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def transcript(db, user):
    """A session with lanes, turns and answers — the shape the hot endpoints read."""
    provider_id = db.scalar(
        select(models.Provider.id).where(models.Provider.user_id == user.id)
    )

    chat = models.Session(user_id=user.id, title="Perf fixture")
    db.add(chat)
    db.flush()

    lanes = []
    for position in range(4):
        lane = models.Lane(
            session_id=chat.id,
            provider_id=provider_id,
            model=f"model-{position}",
            position=position,
        )
        db.add(lane)
        lanes.append(lane)
    db.flush()

    for order in range(12):
        turn = models.Turn(session_id=chat.id, order_index=order, content=f"prompt {order}")
        db.add(turn)
        db.flush()
        for lane in lanes:
            message = models.LaneMessage(
                lane_id=lane.id,
                turn_id=turn.id,
                role="assistant",
                content=f"answer {order} from {lane.model}\n\n" + ("x" * 400),
                order_index=order,
                usage_json={"prompt_tokens": 10, "completion_tokens": 20},
                latency_ms=1200,
            )
            db.add(message)
            db.flush()
            db.add(
                models.ToolCall(
                    lane_message_id=message.id,
                    tool_name="web_search",
                    arguments_json={"query": "x"},
                    result_json={"result": "y" * 9000},
                    status="ok",
                )
            )
    db.commit()
    db.refresh(chat)
    return chat


def reset_engine_counter() -> None:
    engine.dispose()
