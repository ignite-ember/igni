/**
 * TypeScript mirror of src/ember_code/protocol/messages.py.
 *
 * Wire format: one JSON object per WebSocket text frame; the `type`
 * field discriminates. Field names match the Pydantic models exactly —
 * this file must stay in lockstep with the Python side.
 */

export interface BaseMessage {
  type: string;
  /** Correlation id for request/response and stream grouping. */
  id?: string;
  /** Session routing: which session a message targets (FE→BE) or
   *  was emitted by (BE→FE). Empty = default session / global. */
  session_id?: string;
}

// ── BE → FE streaming events ────────────────────────────────────────

export interface ContentDelta extends BaseMessage {
  type: "content_delta";
  text: string;
  is_thinking: boolean;
}

export interface ToolStarted extends BaseMessage {
  type: "tool_started";
  tool_name: string;
  friendly_name: string;
  args_summary: string;
  run_id: string;
}

export interface DiffRow {
  kind: string;
  left_no: string;
  left: string;
  right_no: string;
  right: string;
}

export interface ToolCompleted extends BaseMessage {
  type: "tool_completed";
  summary: string;
  full_result: string;
  run_id: string;
  has_markup: boolean;
  diff_rows: DiffRow[] | null;
  is_error: boolean;
}

export interface ToolError extends BaseMessage {
  type: "tool_error";
  error: string;
  run_id: string;
}

export interface ModelCompleted extends BaseMessage {
  type: "model_completed";
  input_tokens: number;
  output_tokens: number;
  run_id: string;
  parent_run_id: string | null;
}

export interface RunStarted extends BaseMessage {
  type: "run_started";
  agent_name: string;
  run_id: string;
  parent_run_id: string | null;
  model: string;
}

export interface RunCompleted extends BaseMessage {
  type: "run_completed";
  run_id: string;
  parent_run_id: string | null;
  input_tokens: number;
  output_tokens: number;
}

export interface StreamingDone extends BaseMessage {
  type: "streaming_done";
  run_id: string;
}

export interface RunError extends BaseMessage {
  type: "run_error";
  error: string;
}

export interface ReasoningStarted extends BaseMessage {
  type: "reasoning_started";
  run_id: string;
}

export interface HITLRequest extends BaseMessage {
  type: "hitl_request";
  requirement_id: string;
  tool_name: string;
  friendly_name: string;
  tool_args: Record<string, unknown>;
  details: string;
  agent_path: string;
}

export interface RunPaused extends BaseMessage {
  type: "run_paused";
  run_id: string;
  requirements: HITLRequest[];
}

export type CommandResultKind = "markdown" | "info" | "error" | "action";

export type CommandAction =
  | ""
  | "none"
  | "quit"
  | "clear"
  | "sessions"
  | "model"
  | "model_switched"
  | "login"
  | "logout"
  | "help"
  | "mcp"
  | "plugins"
  | "agents"
  | "skills"
  | "knowledge"
  | "codeindex"
  | "hooks"
  | "loop"
  | "schedule"
  | "compact"
  | "run_prompt";

export interface CommandResult extends BaseMessage {
  type: "command_result";
  kind: CommandResultKind;
  content: string;
  action: CommandAction;
  display_content: string;
}

export interface StatusUpdate extends BaseMessage {
  type: "status_update";
  input_tokens: number;
  output_tokens: number;
  context_tokens: number;
  max_context: number;
  model: string;
  cloud_connected: boolean;
  cloud_org: string;
}

export interface Info extends BaseMessage {
  type: "info";
  text: string;
}

export interface ErrorMessage extends BaseMessage {
  type: "error";
  text: string;
}

export interface StreamEnd extends BaseMessage {
  type: "stream_end";
}

export interface RPCResponse extends BaseMessage {
  type: "rpc_response";
  result: unknown;
  error: string | null;
}

export interface PushNotification extends BaseMessage {
  type: "push_notification";
  channel: string;
  payload: Record<string, unknown>;
}

// ── Multi-client session mirroring ──────────────────────────────────

export interface Welcome extends BaseMessage {
  type: "welcome";
  client_id: string;
}

export interface Typing extends BaseMessage {
  type: "typing";
  text: string;
  client_id: string;
}

export interface UserMessageReceived extends BaseMessage {
  type: "user_message_received";
  text: string;
  client_id: string;
  queued: boolean;
}

export interface RequirementResolved extends BaseMessage {
  type: "requirement_resolved";
  requirement_id: string;
}

export type ServerMessage =
  | ContentDelta
  | ToolStarted
  | ToolCompleted
  | ToolError
  | ModelCompleted
  | RunStarted
  | RunCompleted
  | StreamingDone
  | RunError
  | ReasoningStarted
  | HITLRequest
  | RunPaused
  | CommandResult
  | StatusUpdate
  | Info
  | ErrorMessage
  | StreamEnd
  | RPCResponse
  | PushNotification
  | Welcome
  | Typing
  | UserMessageReceived
  | RequirementResolved;

// ── FE → BE messages ────────────────────────────────────────────────

export interface HITLDecision {
  requirement_id: string;
  action: "confirm" | "reject";
  choice: "once" | "always" | "similar" | "";
}

export const fe = {
  userMessage: (
    text: string,
    id: string,
    clientId = "",
    fileContents: Record<string, string> = {},
  ) => ({
    type: "user_message",
    id,
    text,
    file_contents: fileContents,
    client_id: clientId,
  }),
  queueMessage: (text: string, clientId = "") => ({
    type: "queue_message",
    text,
    client_id: clientId,
  }),
  typing: (text: string, clientId: string) => ({
    type: "typing",
    text,
    client_id: clientId,
  }),
  command: (text: string, id: string) => ({ type: "command", id, text }),
  hitlResponse: (requirementId: string, action: string, choice: string, id: string) => ({
    type: "hitl_response",
    id,
    requirement_id: requirementId,
    action,
    choice,
  }),
  hitlResponseBatch: (decisions: HITLDecision[], id: string) => ({
    type: "hitl_response_batch",
    id,
    decisions,
  }),
  cancel: () => ({ type: "cancel" }),
  modelSwitch: (modelName: string, id: string) => ({
    type: "model_switch",
    id,
    model_name: modelName,
  }),
  sessionSwitch: (sessionId: string, id: string) => ({
    type: "session_switch",
    id,
    session_id: sessionId,
  }),
  rpcRequest: (method: string, args: Record<string, unknown>, id: string) => ({
    type: "rpc_request",
    id,
    method,
    args,
  }),
  shutdown: () => ({ type: "shutdown" }),
};
