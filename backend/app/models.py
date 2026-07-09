from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When true, clicking "New chat" immediately starts the default persona instead of
    # showing the persona picker.
    new_chat_use_default_persona: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    providers: Mapped[list["Provider"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    auth_method: Mapped[str] = mapped_column(String, default="api_key", nullable=False)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    models_json: Mapped[list] = mapped_column(JSON, default=list)
    default_model: Mapped[str | None] = mapped_column(String, nullable=True)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    user: Mapped["User"] = relationship(back_populates="providers")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String, default="New topic")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    folder_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    trashed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    user: Mapped["User"] = relationship(back_populates="sessions")
    lanes: Mapped[list["Lane"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Lane(Base):
    __tablename__ = "lanes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String, default="responder")
    state: Mapped[str] = mapped_column(String, default="idle")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    session: Mapped["Session"] = relationship(back_populates="lanes")
    messages: Mapped[list["LaneMessage"]] = relationship(
        back_populates="lane", cascade="all, delete-orphan"
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, default="")
    target_lane_ids_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    session: Mapped["Session"] = relationship(back_populates="turns")
    messages: Mapped[list["LaneMessage"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )


class LaneMessage(Base):
    __tablename__ = "lane_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lane_id: Mapped[str] = mapped_column(
        ForeignKey("lanes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String, default="assistant")
    content: Mapped[str] = mapped_column(Text, default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    lane: Mapped["Lane"] = relationship(back_populates="messages")
    turn: Mapped["Turn"] = relationship(back_populates="messages")
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="lane_message", cascade="all, delete-orphan"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), index=True, nullable=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String, default="image")
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    turn: Mapped["Turn"] = relationship(back_populates="attachments")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lane_message_id: Mapped[str] = mapped_column(
        ForeignKey("lane_messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")
    created_at: Mapped[datetime] = mapped_column(default=_now)

    lane_message: Mapped["LaneMessage"] = relationship(back_populates="tool_calls")


class GeneratedFile(Base):
    __tablename__ = "generated_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    stored_name: Mapped[str] = mapped_column(String, nullable=False)
    download_name: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String, default="file")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AnswerSnapshot(Base):
    """A pinned copy of a model's answer, for comparing the same prompt+model across
    runs over time (model regression at a glance)."""

    __tablename__ = "answer_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String, nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ToolCredential(Base):
    __tablename__ = "tool_credentials"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool: Mapped[str] = mapped_column(String, nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Persona(Base):
    """A reusable topic template: a shared system prompt + a set of lanes."""

    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # [{"provider_id": .., "model": .., "role": ".."}]
    lanes_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Snippet(Base):
    __tablename__ = "snippets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)


class EvalSuite(Base):
    __tablename__ = "eval_suites"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ["prompt one", "prompt two", ...]
    prompts_json: Mapped[list] = mapped_column(JSON, default=list)
    # [{"provider_id": .., "model": ..}]
    models_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    suite_id: Mapped[str] = mapped_column(
        ForeignKey("eval_suites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # [{"prompt": .., "model": .., "answer": .., "score": .., "latency_ms": .., "tokens": ..}]
    results_json: Mapped[list] = mapped_column(JSON, default=list)
    # {"by_model": {model: {"avg_score": .., "avg_latency": .., "count": ..}}}
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Integration(Base):
    """A connected external integration (e.g. Work IQ MCP). Single row per kind/user."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "workiq"
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    eula_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    command: Mapped[str | None] = mapped_column(String, nullable=True)
    args_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)



