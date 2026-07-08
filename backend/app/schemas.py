from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------- Auth ----------


class LoginRequest(BaseModel):
    # allow plain usernames (e.g. "admin") as well as emails
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    created_at: datetime
    # True while the account still uses the seeded default admin/admin credentials, so the
    # UI can prompt the user to change the password.
    is_default_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class AuthResponse(BaseModel):
    token: str
    user: UserOut


# ---------- Providers ----------

ProviderType = Literal[
    "openai",
    "openai_eu",
    "azure_openai",
    "azure_foundry",
    "anthropic",
    "gemini",
    "ollama",
    "openai_compatible",
    "github_copilot",
]
AuthMethod = Literal["api_key", "oauth"]


class ProviderCreate(BaseModel):
    name: str
    provider_type: ProviderType
    auth_method: AuthMethod = "api_key"
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    default_model: str | None = None
    extra: dict[str, Any] | None = None
    is_default: bool = False


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auth_method: AuthMethod | None = None
    models: list[str] | None = None
    default_model: str | None = None
    extra: dict[str, Any] | None = None
    is_default: bool | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    provider_type: ProviderType
    auth_method: AuthMethod
    base_url: str | None
    masked_key: str | None
    has_key: bool
    oauth_connected: bool
    oauth_expires_at: int | None
    oauth_pending: bool = False
    models: list[str]
    default_model: str | None
    extra: dict[str, Any]
    is_default: bool
    created_at: datetime


class TestResult(BaseModel):
    ok: bool
    detail: str


# ---------- OAuth ----------


class OAuthStartOut(BaseModel):
    authorize_url: str | None = None
    flavor: str
    mode: Literal["loopback", "paste", "device"]
    user_code: str | None = None
    verification_uri: str | None = None
    interval: int | None = None
    expires_in: int | None = None


class OAuthCompleteRequest(BaseModel):
    code: str | None = None
    state: str | None = None


class OAuthPollOut(BaseModel):
    status: Literal["pending", "authorized", "error"]
    detail: str | None = None


# ---------- Lanes ----------

LaneRole = Literal["responder", "judge"]


class LaneCreate(BaseModel):
    provider_id: str
    model: str
    role: LaneRole = "responder"


class LaneUpdate(BaseModel):
    provider_id: str | None = None
    model: str | None = None
    position: int | None = None
    hidden: bool | None = None


class LaneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    session_id: str
    provider_id: str
    model: str
    position: int
    role: LaneRole
    state: str
    hidden: bool = False
    created_at: datetime


# ---------- Tool calls / messages ----------


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    tool_name: str
    arguments_json: dict
    result_json: dict | None
    citations_json: list | None
    status: str
    created_at: datetime


class LaneMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    lane_id: str
    turn_id: str
    role: str
    content: str
    order_index: int
    usage_json: dict | None
    latency_ms: int | None
    cost_usd: float | None
    error: str | None
    created_at: datetime
    tool_calls: list[ToolCallOut] = []


class AttachmentOut(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: str = "image"
    url: str


class GeneratedFileOut(BaseModel):
    id: str
    stored_name: str
    download_name: str
    mime_type: str
    size_bytes: int
    kind: str
    url: str
    created_at: datetime


class TurnOut(BaseModel):
    id: str
    session_id: str
    order_index: int
    content: str
    target_lane_ids_json: list | None
    created_at: datetime
    attachments: list[AttachmentOut] = []


# ---------- Sessions ----------


class SessionCreate(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    lanes: list[LaneCreate] = Field(default_factory=list, max_length=6)
    tools_enabled: bool = True
    tool_config: dict[str, Any] | None = None


class SessionUpdate(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    tools_enabled: bool | None = None
    tool_config: dict[str, Any] | None = None
    folder_id: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    trashed: bool | None = None


class SessionListItem(BaseModel):
    id: str
    title: str
    updated_at: datetime
    lane_count: int
    message_count: int = 0
    folder_id: str | None = None
    pinned: bool = False
    archived: bool = False
    trashed: bool = False


class SessionDetail(BaseModel):
    id: str
    title: str
    system_prompt: str | None
    tools_enabled: bool
    tool_config_json: dict
    folder_id: str | None = None
    pinned: bool = False
    archived: bool = False
    created_at: datetime
    updated_at: datetime
    lanes: list[LaneOut]
    turns: list[TurnOut]
    messages: list[LaneMessageOut]


# ---------- Broadcast ----------


class BroadcastRequest(BaseModel):
    content: str
    attachment_ids: list[str] | None = None
    target_lane_ids: list[str] | None = None


class RegenerateRequest(BaseModel):
    turn_id: str | None = None


class JudgeRequest(BaseModel):
    turn_id: str


# ---------- Tools ----------


class ToolDefOut(BaseModel):
    name: str
    description: str
    parameters: dict


class ToolCredentialUpdate(BaseModel):
    tool: str
    api_key: str | None = None
    extra: dict[str, Any] | None = None


class ToolCredentialOut(BaseModel):
    tool: str
    masked_key: str | None
    has_key: bool
    extra: dict[str, Any]


# ---------- Personas ----------


class PersonaLane(BaseModel):
    provider_id: str
    model: str
    role: LaneRole = "responder"
    collapsed: bool = False


class PersonaCreate(BaseModel):
    name: str
    description: str | None = None
    system_prompt: str | None = None
    tools_enabled: bool = True
    lanes: list[PersonaLane] = Field(default_factory=list, max_length=6)


class PersonaUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    tools_enabled: bool | None = None
    lanes: list[PersonaLane] | None = None


class PersonaOut(BaseModel):
    id: str
    name: str
    description: str | None
    system_prompt: str | None
    tools_enabled: bool
    is_default: bool = False
    lanes: list[PersonaLane]
    created_at: datetime
    updated_at: datetime


class PersonaEnhanceIn(BaseModel):
    mode: str = "enhance"  # "enhance" | "generate"
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    instruction: str | None = None  # optional extra guidance from the user
    provider_id: str | None = None  # override; defaults to the user's default provider
    model: str | None = None


class PersonaEnhanceOut(BaseModel):
    system_prompt: str
    used_provider: str
    used_model: str


class AutoTitleOut(BaseModel):
    title: str


# ---------- Folders / Snippets / Settings ----------


class FolderCreate(BaseModel):
    name: str


class FolderUpdate(BaseModel):
    name: str | None = None
    position: int | None = None


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    position: int
    created_at: datetime


class SnippetCreate(BaseModel):
    title: str
    content: str = ""


class SnippetUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


class SnippetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    content: str
    created_at: datetime


class UserSettings(BaseModel):
    custom_instructions: str | None = None


class SearchHit(BaseModel):
    session_id: str
    title: str
    snippet: str
    updated_at: datetime

