/**
 * FE↔BE wire contract — every field the web client reads must exist
 * in the BE-generated schema snapshot. 4 of the 11 bugs found in the
 * 2026-06-11 verification sweep were silent field-name mismatches
 * (DiffRow shape, is_ephemeral, loop_status fields, pending content);
 * this test turns the next one into a red test instead of a blank UI.
 *
 * Regenerate the snapshot after BE protocol changes:
 *     uv run python scripts/dump_wire_schema.py
 */
import { describe, expect, it } from "vitest";
import schema from "./wire-schema.json";

const messages = schema.messages as Record<string, string[]>;
const rpc = schema.rpc as Record<string, string[]>;

/** type → fields the FE actually reads (grep the consumer if it fails). */
const MESSAGE_READS: Record<string, string[]> = {
  welcome: ["client_id"],
  user_message_received: ["text", "client_id"],
  content_delta: ["text", "is_thinking"],
  tool_started: ["tool_name", "friendly_name", "args_summary", "run_id"],
  tool_completed: ["summary", "full_result", "run_id", "has_markup", "diff_rows", "is_error"],
  stream_end: ["id"],
  run_completed: ["input_tokens", "output_tokens", "reasoning_tokens", "duration", "parent_run_id"],
  run_paused: ["requirements"],
  requirement_resolved: ["requirement_id"],
  status_update: ["model", "context_tokens", "max_context", "permission_mode"],
  session_list_result: ["sessions"],
  rpc_response: ["id", "result"],
  push_notification: ["channel", "payload"],
};

const RPC_READS: Record<string, string[]> = {
  loop_status: [
    "active",
    "paused",
    "prompt",
    "iteration_index",
    "iterations_remaining",
    "cap_explicit",
    "announced_total",
  ],
  pending_message: ["role", "content"],
  mcp_server: [
    "name",
    "connected",
    "transport",
    "tool_names",
    "tool_descriptions",
    "resources",
    "prompts",
    "error",
    "policy_blocked",
  ],
  agent_info: ["name", "description", "model", "is_ephemeral", "can_orchestrate"],
  scheduled_task: ["id", "description", "scheduled_at", "status", "recurrence"],
};

describe("wire contract", () => {
  it.each(Object.entries(MESSAGE_READS))("message %s has the fields the FE reads", (type, reads) => {
    const fields = messages[type];
    expect(fields, `message type "${type}" missing from wire-schema.json`).toBeDefined();
    for (const f of reads) expect(fields, `${type}.${f}`).toContain(f);
  });

  it.each(Object.entries(RPC_READS))("rpc payload %s has the fields the FE reads", (key, reads) => {
    const fields = rpc[key];
    expect(fields, `rpc payload "${key}" missing from wire-schema.json`).toBeDefined();
    for (const f of reads) expect(fields, `${key}.${f}`).toContain(f);
  });
});
