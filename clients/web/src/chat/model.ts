/**
 * Conversation item model + the reducer applying streamed protocol
 * events. Mirrors the TUI's rendering rules in run_controller._render:
 * content deltas append to the open assistant block, thinking deltas
 * go to a dimmed block, tool events create/update cards.
 */

import type { DiffRow, ServerMessage } from "../protocol/messages";

export type ChatItem =
  | { kind: "user"; id: number; text: string }
  | { kind: "assistant"; id: number; text: string }
  | { kind: "thinking"; id: number; text: string }
  | {
      kind: "tool";
      id: number;
      runId: string;
      name: string;
      args: string;
      status: "running" | "done" | "error";
      result: string;
      isError: boolean;
      diffRows: DiffRow[] | null;
    }
  | { kind: "agent"; id: number; text: string }
  | { kind: "info"; id: number; text: string }
  | { kind: "error"; id: number; text: string }
  | { kind: "shell"; id: number; command: string; output: string; exitCode: number | null };

let itemId = 0;
const nid = () => ++itemId;

export function shellItem(command: string): ChatItem {
  return { kind: "shell", id: nid(), command, output: "", exitCode: null };
}

/**
 * Streaming <think>-tag parser state. Some models (e.g. MiniMax) wrap
 * reasoning in literal <think>…</think> tags inside the content
 * stream instead of using separate reasoning events. Mirrors the
 * TUI's rules (run_controller._on_content_chunk):
 *
 * - `<think>` flips to thinking mode; `</think>` flips back.
 * - After a tool call, these models resume thinking WITHOUT an
 *   opening tag (only emitting `</think>` later), so tool boundaries
 *   pre-enter thinking mode — but only once the model has proven it
 *   uses think tags (`usesThinkTags`).
 * - A tag may arrive split across deltas; `carry` buffers a trailing
 *   partial tag (e.g. "…<thi") until the next delta resolves it.
 */
export interface StreamState {
  inThinking: boolean;
  usesThinkTags: boolean;
  carry: string;
}

export function newStreamState(): StreamState {
  return { inThinking: false, usesThinkTags: false, carry: "" };
}

const OPEN_TAG = "<think>";
const CLOSE_TAG = "</think>";

/** Longest suffix of `s` that is a proper prefix of an open/close tag. */
function partialTagSuffix(s: string): string {
  for (let len = Math.min(s.length, CLOSE_TAG.length - 1); len > 0; len--) {
    const tail = s.slice(-len);
    if (OPEN_TAG.startsWith(tail) || CLOSE_TAG.startsWith(tail)) return tail;
  }
  return "";
}

/**
 * Split one content delta into [text, isThinking] segments, updating
 * `state` in place. Exposed for tests.
 */
export function splitThinkTags(state: StreamState, delta: string): [string, boolean][] {
  let text = state.carry + delta;
  state.carry = "";
  const out: [string, boolean][] = [];

  for (;;) {
    const tag = state.inThinking ? CLOSE_TAG : OPEN_TAG;
    const at = text.indexOf(tag);
    if (at === -1) break;
    const before = text.slice(0, at);
    if (before) out.push([before, state.inThinking]);
    if (!state.inThinking) state.usesThinkTags = true;
    state.inThinking = !state.inThinking;
    text = text.slice(at + tag.length);
    // Content resuming after </think> starts with cosmetic newlines.
    if (!state.inThinking) text = text.replace(/^\n+/, "");
  }

  // Models that use think tags close without reopening; a bare
  // </think> can also arrive when we never saw <think> (post-tool
  // resume). Treat a stray close tag while NOT thinking as a no-op
  // boundary rather than rendering it literally.
  if (!state.inThinking && text.includes(CLOSE_TAG)) {
    const [before, after] = [
      text.slice(0, text.indexOf(CLOSE_TAG)),
      text.slice(text.indexOf(CLOSE_TAG) + CLOSE_TAG.length),
    ];
    if (before) out.push([before, true]);
    text = after.replace(/^\n+/, "");
  }

  const partial = partialTagSuffix(text);
  if (partial) {
    state.carry = partial;
    text = text.slice(0, text.length - partial.length);
  }
  if (text) out.push([text, state.inThinking]);
  return out;
}

/** Tool boundary: reset thinking, pre-enter if the model uses tags. */
export function onToolBoundary(state: StreamState): void {
  state.carry = "";
  state.inThinking = state.usesThinkTags;
}

export function userItem(text: string): ChatItem {
  return { kind: "user", id: nid(), text };
}

export function infoItem(text: string): ChatItem {
  return { kind: "info", id: nid(), text };
}

export function errorItem(text: string): ChatItem {
  return { kind: "error", id: nid(), text };
}

export function assistantItem(text: string): ChatItem {
  return { kind: "assistant", id: nid(), text };
}

function appendText(items: ChatItem[], text: string, thinking: boolean): ChatItem[] {
  if (!text) return items;
  const wantKind = thinking ? "thinking" : "assistant";
  const last = items[items.length - 1];
  if (last && last.kind === wantKind) {
    const updated = { ...last, text: last.text + text };
    return [...items.slice(0, -1), updated];
  }
  return [...items, { kind: wantKind, id: nid(), text } as ChatItem];
}

/**
 * Apply one streamed event to the item list, returning a new list.
 * Pure w.r.t. items; `stream` (the <think> parser state) is mutated.
 */
export function applyEvent(
  items: ChatItem[],
  msg: ServerMessage,
  stream: StreamState = newStreamState(),
): ChatItem[] {
  switch (msg.type) {
    case "content_delta": {
      if (!msg.text) return items;
      // Agno-level reasoning events are already tagged; inline
      // <think> tags need the streaming parser.
      if (msg.is_thinking) return appendText(items, msg.text, true);
      let next = items;
      for (const [seg, thinking] of splitThinkTags(stream, msg.text)) {
        next = appendText(next, seg, thinking);
      }
      return next;
    }

    case "tool_started":
      // Tool boundary closes any open thinking block (TUI parity).
      stream.inThinking = false;
      stream.carry = "";
      return [
        ...items,
        {
          kind: "tool",
          id: nid(),
          runId: msg.run_id,
          name: msg.friendly_name || msg.tool_name,
          args: msg.args_summary,
          status: "running",
          result: "",
          isError: false,
          diffRows: null,
        },
      ];

    case "tool_completed": {
      // Models that use <think> tags resume thinking after a tool
      // WITHOUT re-opening the tag — pre-enter thinking mode.
      onToolBoundary(stream);
      // Update the most recent running tool card for this run.
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === "tool" && it.status === "running") {
          const updated: ChatItem = {
            ...it,
            status: msg.is_error ? "error" : "done",
            result: msg.full_result || msg.summary,
            isError: msg.is_error,
            diffRows: msg.diff_rows ?? null,
          };
          return [...items.slice(0, i), updated, ...items.slice(i + 1)];
        }
      }
      return items;
    }

    case "tool_error": {
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === "tool" && it.status === "running") {
          const updated: ChatItem = { ...it, status: "error", result: msg.error, isError: true };
          return [...items.slice(0, i), updated, ...items.slice(i + 1)];
        }
      }
      return [...items, errorItem(msg.error)];
    }

    case "run_started":
      // Sub-agent dispatch marker (the main run has no parent).
      if (msg.parent_run_id) {
        return [...items, { kind: "agent", id: nid(), text: `→ ${msg.agent_name}` }];
      }
      return items;

    case "run_error":
      return [...items, errorItem(msg.error)];

    case "info":
      return [...items, infoItem(msg.text)];

    case "error":
      return [...items, errorItem(msg.text)];

    default:
      return items;
  }
}
