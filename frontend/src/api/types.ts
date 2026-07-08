export interface User {
  id: string;
  email: string;
  created_at: string;
  is_default_password?: boolean;
}

export type ProviderType =
  | "openai"
  | "openai_eu"
  | "azure_openai"
  | "azure_foundry"
  | "anthropic"
  | "gemini"
  | "ollama"
  | "openai_compatible"
  | "github_copilot";

export type AuthMethod = "api_key" | "oauth";

export interface Provider {
  id: string;
  name: string;
  provider_type: ProviderType;
  auth_method: AuthMethod;
  base_url: string | null;
  masked_key: string | null;
  has_key: boolean;
  oauth_connected: boolean;
  oauth_expires_at: number | null;
  oauth_pending?: boolean;
  models: string[];
  default_model: string | null;
  extra: Record<string, any>;
  is_default: boolean;
  created_at: string;
}

export type LaneRole = "responder" | "judge";

export interface Lane {
  id: string;
  session_id: string;
  provider_id: string;
  model: string;
  position: number;
  role: LaneRole;
  state: string;
  hidden?: boolean;
  created_at: string;
}

export interface ToolCall {
  id: string;
  tool_name: string;
  arguments_json: Record<string, any>;
  result_json: Record<string, any> | null;
  citations_json: any[] | null;
  status: string;
  created_at: string;
}

export interface LaneMessage {
  id: string;
  lane_id: string;
  turn_id: string;
  role: string;
  content: string;
  order_index: number;
  usage_json: Record<string, any> | null;
  latency_ms: number | null;
  cost_usd: number | null;
  error: string | null;
  created_at: string;
  tool_calls: ToolCall[];
}

export interface Attachment {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  kind?: string;
  url: string;
}

export interface GeneratedFile {
  id: string;
  stored_name: string;
  download_name: string;
  mime_type: string;
  size_bytes: number;
  kind: string;
  url: string;
  created_at: string;
}

export interface Turn {
  id: string;
  session_id: string;
  order_index: number;
  content: string;
  target_lane_ids_json: string[] | null;
  created_at: string;
  attachments: Attachment[];
}

export interface SessionListItem {
  id: string;
  title: string;
  updated_at: string;
  lane_count: number;
  message_count: number;
  folder_id: string | null;
  pinned: boolean;
  archived: boolean;
  trashed: boolean;
}

export interface SessionDetail {
  id: string;
  title: string;
  system_prompt: string | null;
  tools_enabled: boolean;
  tool_config_json: Record<string, any>;
  folder_id: string | null;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  updated_at: string;
  lanes: Lane[];
  turns: Turn[];
  messages: LaneMessage[];
}

export interface ToolDef {
  name: string;
  description: string;
  parameters: Record<string, any>;
}

export interface ToolCredential {
  tool: string;
  masked_key: string | null;
  has_key: boolean;
  extra: Record<string, any>;
}

export interface PersonaLane {
  provider_id: string;
  model: string;
  role: LaneRole;
  collapsed?: boolean;
}

export interface Persona {
  id: string;
  name: string;
  description: string | null;
  system_prompt: string | null;
  tools_enabled: boolean;
  is_default?: boolean;
  lanes: PersonaLane[];
  created_at: string;
  updated_at: string;
}

export interface Folder {
  id: string;
  name: string;
  position: number;
  created_at: string;
}

export interface Snippet {
  id: string;
  title: string;
  content: string;
  created_at: string;
}

export interface SearchHit {
  session_id: string;
  title: string;
  snippet: string;
  updated_at: string;
}

export interface TestResult {
  ok: boolean;
  detail: string;
}

// SSE event payloads
export interface LaneStartEvent {
  lane_id: string;
  turn_id: string;
}
export interface ChunkEvent {
  lane_id: string;
  delta: string;
}
export interface ToolCallEvent {
  lane_id: string;
  tool_call_id: string;
  tool: string;
  arguments: Record<string, any>;
}
export interface ToolResultEvent {
  lane_id: string;
  tool_call_id: string;
  status: string;
  result: string;
  citations: any[] | null;
}
export interface LaneDoneEvent {
  lane_id: string;
  message: { id: string; content: string };
  usage: Record<string, any>;
  latency_ms: number;
  cost_usd: number;
}
export interface LaneErrorEvent {
  lane_id: string;
  detail: string;
}
