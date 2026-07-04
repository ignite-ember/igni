/**
 * Conversation item model + the reducer applying streamed protocol
 * events. Mirrors the TUI's rendering rules in run_controller._render:
 * content deltas append to the open assistant block, thinking deltas
 * go to a dimmed block, tool events create/update cards.
 */

import type { DiffRow, ServerMessage } from "../protocol/messages";

/** One sub-agent's slice of an orchestrate run. Tool calls keep
 *  status so a partial run shows which tools are still running. The
 *  content preview is the latest "✎" line — overwritten on each new
 *  event so the card doesn't fill with reasoning chatter. */
export interface OrchestrateAgent {
  path: string;
  name: string;
  parent: string | null;
  status: "running" | "done" | "error" | "paused";
  /** Agno run_id for this sub-agent's run — emitted by the BE on
   *  ``agent_started`` for team members. Empty for the standalone
   *  ``spawn_agent`` path (no parallel siblings to stop). Used by
   *  the per-agent Stop button to call ``cancel_agent_run``. */
  runId: string;
  /** Original task prompt the parent gave this sub-agent. Carried
   *  on ``agent_started`` so the Retry UI can pre-fill the textarea
   *  with what was originally asked — the user tweaks it, the
   *  retry sends a follow-up message asking the main agent to
   *  re-fire this specialist with the new task. */
  task: string;
  /** Rolling window of the agent's last few "thinking" lines. We
   *  keep a tail (not just the latest) so a user opening the card
   *  mid-run sees a few seconds of context, not just the snapshot
   *  of whatever the agent happened to be saying RIGHT now.
   *  Capped — older entries drop off the front. */
  previewLines: string[];
  tools: OrchestrateToolCall[];
  /** Token totals from this agent's RunCompletedEvent metrics.
   *  Zero while the run is still in flight — populated once the
   *  agent finishes (or errors). Summed across agents to drive the
   *  team-level header total. */
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
}

/** Cap on how many preview lines we keep per agent. 5 lines is enough
 *  to see a coherent thought without consuming bubble height — and
 *  matches the BE's ``PREVIEW_WINDOW`` in
 *  ``core/tools/orchestrate.py`` (the BE is the source of truth for
 *  the window contents). */
export const PREVIEW_WINDOW = 5;

export interface OrchestrateToolCall {
  id: number;
  /** Agno-side tool execution id — used to match the completion
   *  event to its started event when many overlap. May be missing
   *  on older BE builds; we fall back to "last running with same
   *  tool name" in that case. */
  toolCallId?: string;
  tool: string;
  args: string;
  status: "running" | "done" | "error";
  result: string;
}

/** True if any agent in this orchestrate card is still actively
 *  working — used to decide whether new events should fold into the
 *  card or start a fresh one. Once every agent is in a terminal
 *  state (done / error / paused), the card is "settled". */
export function isOrchestrateActive(
  agents: Record<string, OrchestrateAgent>,
  order: string[],
): boolean {
  if (order.length === 0) return true; // empty card has nothing to settle yet
  return order.some((p) => {
    const a = agents[p];
    return a && (a.status === "running" || a.status === "paused");
  });
}

/** Discriminated union of the events the BE emits over the
 *  ``orchestrate_event`` push channel. Mirrors the dicts produced by
 *  ``orchestrate.py``. */
export type OrchestrateEvent =
  | {
      type: "agent_started";
      agent_path: string;
      agent: string;
      parent?: string | null;
      run_id?: string;
      task?: string;
    }
  | { type: "agent_paused"; agent_path: string; count?: number }
  | {
      type: "agent_completed";
      agent_path: string;
      is_error?: boolean;
      input_tokens?: number;
      output_tokens?: number;
      reasoning_tokens?: number;
    }
  | {
      type: "tool_started";
      agent_path: string;
      tool: string;
      tool_call_id?: string | null;
      args: string;
    }
  | {
      type: "tool_completed";
      agent_path: string;
      tool: string;
      tool_call_id?: string | null;
      result: string;
      is_error?: boolean;
    }
  | { type: "content_preview"; agent_path: string; text: string }
  | { type: "run_error"; agent_path: string; error: string }
  | { type: "task_created"; agent_path: string; title: string; assignee?: string }
  | { type: "task_updated"; agent_path: string; status: string };

/** Fold one orchestrate event into the running agent map. Pure: takes
 *  the previous agents map + the event, returns a new map. Caller is
 *  responsible for tracking insertion order. */
export function applyOrchestrateEvent(
  agents: Record<string, OrchestrateAgent>,
  order: string[],
  ev: OrchestrateEvent,
): { agents: Record<string, OrchestrateAgent>; order: string[] } {
  const ensure = (path: string, name?: string, parent?: string | null): OrchestrateAgent => {
    if (agents[path]) return agents[path];
    const fallbackName = name || path.split(".").pop() || path;
    const created: OrchestrateAgent = {
      path,
      name: fallbackName,
      parent: parent ?? null,
      status: "running",
      previewLines: [],
      tools: [],
      inputTokens: 0,
      outputTokens: 0,
      reasoningTokens: 0,
      runId: "",
      task: "",
    };
    agents = { ...agents, [path]: created };
    if (!order.includes(path)) order = [...order, path];
    return created;
  };

  switch (ev.type) {
    case "agent_started": {
      const a = ensure(ev.agent_path, ev.agent, ev.parent ?? null);
      const next = { ...a };
      let touched = false;
      if (ev.run_id && !a.runId) {
        next.runId = ev.run_id;
        touched = true;
      }
      if (ev.task && !a.task) {
        next.task = ev.task;
        touched = true;
      }
      if (touched) agents = { ...agents, [a.path]: next };
      return { agents, order };
    }
    case "agent_paused": {
      const a = ensure(ev.agent_path);
      agents = { ...agents, [a.path]: { ...a, status: "paused" } };
      return { agents, order };
    }
    case "agent_completed": {
      const a = ensure(ev.agent_path);
      agents = {
        ...agents,
        [a.path]: {
          ...a,
          status: ev.is_error ? "error" : "done",
          // Keep previously-known totals if the BE didn't ship
          // metrics (older BEs, or an error event that didn't
          // route through Agno's metrics machinery).
          inputTokens: ev.input_tokens ?? a.inputTokens,
          outputTokens: ev.output_tokens ?? a.outputTokens,
          reasoningTokens: ev.reasoning_tokens ?? a.reasoningTokens,
        },
      };
      return { agents, order };
    }
    case "tool_started": {
      const a = ensure(ev.agent_path);
      const tool: OrchestrateToolCall = {
        id: nid(),
        toolCallId: ev.tool_call_id || undefined,
        tool: ev.tool,
        args: ev.args,
        status: "running",
        result: "",
      };
      agents = { ...agents, [a.path]: { ...a, tools: [...a.tools, tool] } };
      return { agents, order };
    }
    case "tool_completed": {
      const a = ensure(ev.agent_path);
      // Prefer matching by Agno's tool_call_id when present — that
      // disambiguates overlapping calls (e.g. an agent firing two
      // run_shell_command in parallel). Fall back to "last running
      // with same name" when id isn't carried.
      const tools = a.tools.slice();
      const targetId = ev.tool_call_id || undefined;
      let matched = false;
      if (targetId) {
        for (let i = tools.length - 1; i >= 0; i--) {
          if (tools[i].toolCallId === targetId) {
            tools[i] = {
              ...tools[i],
              status: ev.is_error ? "error" : "done",
              result: ev.result,
            };
            matched = true;
            break;
          }
        }
      }
      if (!matched) {
        for (let i = tools.length - 1; i >= 0; i--) {
          if (tools[i].tool === ev.tool && tools[i].status === "running") {
            tools[i] = {
              ...tools[i],
              status: ev.is_error ? "error" : "done",
              result: ev.result,
            };
            break;
          }
        }
      }
      agents = { ...agents, [a.path]: { ...a, tools } };
      return { agents, order };
    }
    case "content_preview": {
      const a = ensure(ev.agent_path);
      // The BE owns the preview window: ``text`` is the last ~5
      // non-empty lines of the agent's accumulated streaming content,
      // joined by ``\n``. We split + replace rather than append so the
      // FE never has to reconstruct lines from token-sized deltas —
      // see ``_build_preview`` in core/tools/orchestrate.py.
      const raw = ev.text || "";
      const parsed = raw
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const next = parsed.slice(-PREVIEW_WINDOW);
      if (next.length === 0) return { agents, order };
      // Skip the rerender if the window is byte-identical to what we
      // already have — orchestrate events fire fast enough that this
      // dedup is what keeps the chat tree from re-rendering 20x/sec
      // when an agent is mid-thought.
      if (
        a.previewLines.length === next.length &&
        a.previewLines.every((line, i) => line === next[i])
      ) {
        return { agents, order };
      }
      agents = { ...agents, [a.path]: { ...a, previewLines: next } };
      return { agents, order };
    }
    case "run_error": {
      const a = ensure(ev.agent_path);
      // Show the error in the preview tail too, so users see what
      // killed the agent even with the body collapsed.
      const lines = [...a.previewLines, `ERROR: ${ev.error}`];
      if (lines.length > PREVIEW_WINDOW) lines.splice(0, lines.length - PREVIEW_WINDOW);
      agents = {
        ...agents,
        [a.path]: { ...a, status: "error", previewLines: lines },
      };
      return { agents, order };
    }
    case "task_created":
    case "task_updated":
      ensure(ev.agent_path);
      return { agents, order };
  }
}

/** One structured task from the agent's ``exit_plan_mode``
 *  ``tasks=[...]`` argument, plus the live status the
 *  ``todos_updated`` push refreshes during execution.
 *  ``activeForm`` is the verb-noun gerund the UI renders while
 *  the task is ``in_progress`` (e.g. "Running tests" instead of
 *  "Run tests"). */
export type PlanTask = {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
};

/** Coerce one untrusted task object (from a ``plan_submitted`` or
 *  ``todos_updated`` push) to a {@link PlanTask}. Returns ``null``
 *  when the object lacks a non-empty ``content`` field — those are
 *  unusable and the caller should drop them. ``status`` defaults to
 *  ``"pending"`` if missing or not one of the three valid values
 *  (the BE could send a future status we don't recognise yet —
 *  fail safe rather than crash). */
export function normalizePlanTask(raw: unknown): PlanTask | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const content = String(r.content ?? "").trim();
  if (!content) return null;
  const status =
    r.status === "in_progress" || r.status === "completed" ? r.status : "pending";
  const activeForm = String(r.activeForm ?? "").trim();
  return { content, status, activeForm };
}

/** Bulk version of {@link normalizePlanTask} that silently skips
 *  any element it can't normalize. Used by the ``plan_submitted``
 *  channel and ``todos_updated`` channel — both arrive as a list. */
export function normalizePlanTasks(raw: unknown): PlanTask[] {
  if (!Array.isArray(raw)) return [];
  const out: PlanTask[] = [];
  for (const t of raw) {
    const n = normalizePlanTask(t);
    if (n) out.push(n);
  }
  return out;
}

/** Merge a fresh ``todos_updated`` snapshot into an existing
 *  PlanCard's task list, matching by ``content``.
 *
 *  - Existing tasks not present in ``todos`` keep their last-known
 *    status (the agent may have dropped a step mid-flight; blanking
 *    would surprise the user worse than a stale tick).
 *  - New tasks in ``todos`` that aren't in the original plan are
 *    NOT added — the PlanCard reflects the plan the user
 *    approved, not the live todo set. (CC behavior; the bare
 *    ``/todos`` view shows the latter.)
 *  - ``activeForm`` only updates if the new value is non-empty,
 *    so a partial update can't erase a previously-set gerund. */
export function mergePlanTasks(existing: PlanTask[], todos: unknown): PlanTask[] {
  if (existing.length === 0) return existing;
  const fresh = normalizePlanTasks(todos);
  if (fresh.length === 0) return existing;
  const byContent = new Map<string, PlanTask>();
  for (const t of fresh) byContent.set(t.content, t);
  return existing.map((task) => {
    const match = byContent.get(task.content);
    if (!match) return task;
    return {
      ...task,
      status: match.status,
      activeForm: match.activeForm || task.activeForm,
    };
  });
}

export type ChatItem =
  | {
      kind: "user";
      id: number;
      text: string;
      /** Run that owns this user message. Set on history restore from
       *  the BE; backfilled live when ``run_started`` fires. Required
       *  for the edit/delete operations — both call
       *  ``truncate_history(runId)`` to wipe the run + everything
       *  after it before continuing. */
      runId?: string;
      /** Wall-clock epoch (ms) when the run started. Stamped on
       *  ``run_started``. Used together with ``streamingEndedAt`` to
       *  surface the user-perceived response time on the stats line —
       *  Agno's ``duration`` includes its post-stream tail (memory
       *  extraction, persistence, etc.) which can add 5-15s of "doing
       *  nothing" after the answer is visible. */
      startedAt?: number;
      /** Wall-clock epoch (ms) when ``streaming_done`` fired — i.e.
       *  when the visible answer finished from the user's POV. */
      streamingEndedAt?: number;
    }
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
      /** Name of the agent that called this tool. Set when the
       *  StreamState knows which sub-agent's run_id owns the tool —
       *  rendered as a small badge in ToolCard so broadcast outputs
       *  are attributable. Top-level main-team tools leave this
       *  empty (no badge needed). */
      agentName?: string;
    }
  | { kind: "agent"; id: number; text: string }
  /** Live team-orchestration progress. The BE emits structured
   *  events (``orchestrate_event`` push notifications) carrying
   *  agent path, tool calls, content previews, status transitions.
   *  We aggregate them into a per-agent tree so the FE can render a
   *  proper collapsible UI instead of ASCII tree art. ``streaming``
   *  flips to false on the owning top-level run_completed so a
   *  collapsed card still signals "work in flight".
   *
   *  ``cardId`` is the BE-stamped per-spawn id (see
   *  ``core/tools/orchestrate.py``). Every event carries it so we can
   *  route to the right card by id instead of guessing "is this the
   *  last item." That makes interleaved info items, page refreshes,
   *  and concurrent team spawns all line up cleanly. */
  | {
      kind: "orchestrate";
      id: number;
      cardId: string;
      agents: Record<string, OrchestrateAgent>;
      order: string[];
      streaming: boolean;
    }
  | { kind: "info"; id: number; text: string }
  | { kind: "error"; id: number; text: string }
  | {
      /** Result of /compact. Rendered as a card with a header line +
       *  the model's summary as markdown body. ``summary`` is empty
       *  when Agno didn't generate one (e.g. nothing to compact, or
       *  the summarizer call failed) — the card still renders with a
       *  honest "(no summary generated)" placeholder. */
      kind: "compact";
      id: number;
      status: string;
      summary: string;
    }
  | {
      /** Plan submitted by the agent via ``exit_plan_mode`` (row 50).
       *  Renders the plan markdown + Approve / Reject buttons.
       *
       *  ``state`` transitions: ``pending`` → ``approved`` (user
       *  clicked Approve, which flips ``/plan off``) or
       *  ``dismissed`` (user clicked Reject — buttons hide; the
       *  plan body stays visible so the agent can still reference
       *  it in the conversation transcript). The card never
       *  disappears entirely — the plan is part of the
       *  conversation history.
       *
       *  ``tasks`` is the optional structured checklist the
       *  agent passed with ``exit_plan_mode(plan, tasks=[...])``.
       *  Seeded from the ``plan_submitted`` push; updated in
       *  place as ``todos_updated`` pushes arrive during
       *  execution (matched by ``content``). Empty when the
       *  agent submitted a prose-only plan. */
      kind: "plan";
      id: number;
      plan: string;
      state: "pending" | "approved" | "dismissed";
      tasks: PlanTask[];
      /** Run that submitted the plan. Used as the key when the
       *  FE calls ``approve_plan`` / ``dismiss_plan`` so the BE
       *  can persist the per-plan decision instead of inferring
       *  from permission mode. Empty when the BE didn't supply
       *  one (legacy push without ``run_id``); approve/dismiss
       *  silently no-ops to keep the FE from spamming an empty
       *  key the BE would reject. */
      runId: string;
    }
  | {
      /** Structured marker for one ``/loop`` iteration. The wrapped
       *  prompt sent to the model is verbose (autonomous-mode
       *  instructions + ``loop_set_total`` reminder); the chat just
       *  needs the iteration number and the user's original ask. */
      kind: "loop";
      id: number;
      index: number;
      total: number | null;
      body: string;
      /** The full wrapped prompt — kept so a "show prompt" toggle in
       *  the renderer can reveal what the model actually received. */
      raw: string;
    }
  | { kind: "shell"; id: number; command: string; output: string; exitCode: number | null }
  | {
      kind: "attachments";
      id: number;
      files: { name: string; path: string }[];
    }
  /** Stats roll-up emitted at the end of a top-level run. Stored
   *  structurally so the input number can be patched once the BE
   *  reports the corrected ``count_context_tokens`` — Agno's
   *  ``input_tokens`` from RunCompleted sums across model iterations
   *  and inflates 2-3× compared to the actual session size. */
  /** A json-render UI spec emitted by the visualizer sub-agent via the
   *  ``visualization`` push channel. Rendered inline via
   *  ``<JsonRenderView spec={...} />``. Payloads travel one-way from
   *  BE→FE; the FE never sends them back.
   *
   *  ``specId`` is the BE-stamped dedupe key (one per visualizer
   *  run). Repeated visualize() calls with the same ``specId`` update
   *  the existing card in place instead of appending a new one — the
   *  streaming-update path. Different id = different card. */
  | {
      kind: "visualization";
      id: number;
      specId: string;
      /** The json-render flat-tree spec. Passed through verbatim to
       *  ``@json-render/react``'s ``Renderer`` — this component treats
       *  the object reference as immutable (memoized on identity). */
      spec: unknown;
      /** Optional short label shown above the card. */
      title: string;
      /** Which agent emitted the spec (currently always ``visualizer``,
       *  but kept as a string so future callers can attribute). */
      sourceAgent: string;
    }
  | {
      kind: "stats";
      id: number;
      runId: string;
      /** Billed input (corrected post-run via count_context_tokens). */
      inputTokens: number;
      /** Billed output total (kept for tooltip — `output - reasoning`
       *  includes invisible scratch tokens, so we don't display this
       *  directly). */
      outputTokens: number;
      /** Billed reasoning tokens from Agno (kept for tooltip; doesn't
       *  always match the visible "thinking" block size — the model
       *  bills more thinking than it streams back). */
      reasoningTokens: number;
      /** Estimated tokens for the rendered "thinking" content of this
       *  turn (~chars/4). What we show as "think" in the stats line. */
      visibleThinkTokens: number;
      /** Estimated tokens for the visible assistant reply (~chars/4).
       *  What we show as "out". */
      visibleOutTokens: number;
      duration: number;
      /** Set to true once App.tsx has overwritten ``inputTokens`` with
       *  the real session size from ``count_context_tokens``. */
      corrected: boolean;
    };

let itemId = 0;
const nid = () => ++itemId;

export function shellItem(command: string): ChatItem {
  return { kind: "shell", id: nid(), command, output: "", exitCode: null };
}

export function planItem(plan: string, tasks: PlanTask[] = [], runId = ""): ChatItem {
  return { kind: "plan", id: nid(), plan, state: "pending", tasks, runId };
}

export function attachmentsItem(files: { name: string; path: string }[]): ChatItem {
  return { kind: "attachments", id: nid(), files };
}

export function visualizationItem(
  spec: unknown,
  title: string,
  sourceAgent: string,
  specId: string,
): ChatItem {
  return { kind: "visualization", id: nid(), specId, spec, title, sourceAgent };
}

/** Pull file paths out of a restored user message — used to rebuild
 *  the attachment cards above the bubble on reload. Checks the
 *  ``[Referenced files: …]`` hint line the BE writes inside the
 *  ``<attached-files>`` wrapper first, then falls back to any
 *  surviving ``@path`` mentions in the body. */
export function extractAttachedPaths(content: string): string[] {
  const fromHint = content.match(REFS_LINE_RE);
  if (fromHint) {
    return fromHint[1]
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  const paths: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = AT_PATH_RE.exec(content)) !== null) paths.push(m[1]);
  return paths;
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
  /** Map of Agno ``run_id`` → agent name. Populated on every
   *  ``run_started`` event (top-level + sub-agents). Tool events
   *  carry their owning ``run_id`` but not the agent name, so we
   *  consult this map to attribute tool cards to the agent that
   *  ran them during a broadcast. */
  runToAgent: Map<string, string>;
  /** Wall-clock (ms) when the last visible ``content_delta`` arrived.
   *  ``streaming_done`` reads this so the message timestamp reflects
   *  when the last chunk actually landed, not when the (later)
   *  ``streaming_done`` signal fired — the two can drift by hundreds
   *  of ms on slow networks. ``ContentDelta`` itself carries no
   *  ``run_id`` on the wire, but the BE serialises runs (one
   *  ``_run_lock`` per session), so a single rolling value is safe:
   *  ``streaming_done`` consumes and resets it. */
  lastContentAt?: number;
}

export function newStreamState(): StreamState {
  return {
    inThinking: false,
    usesThinkTags: false,
    carry: "",
    runToAgent: new Map(),
  };
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

/** Parse a wrapped ``/loop`` iteration prompt. The BE emits a
 *  predictable shape (see ``core/loop/prompt.py``); pull out the
 *  iteration index, optional total, and the user's original ask
 *  underneath the autonomous-mode boilerplate. Returns ``null`` if
 *  the input isn't a loop wrapper — caller can fall back to a plain
 *  info item. */
export function parseLoopIteration(
  wrapped: string,
): { index: number; total: number | null; body: string } | null {
  const open = wrapped.match(/^<loop-iteration\s+index="(\d+)"(?:\s+total="(\d+)")?>\s*\n/);
  if (!open) return null;
  const close = wrapped.lastIndexOf("</loop-iteration>");
  if (close < 0) return null;
  const inner = wrapped.slice(open[0].length, close).trimEnd();
  // Strip the two standard boilerplate paragraphs — match by their
  // known opening phrases so blank lines inside the user's prompt
  // don't get eaten.
  const body = inner
    .replace(/^Autonomous loop iteration[\s\S]*?\n\n/, "")
    .replace(/^When you can determine[\s\S]*?\n\n/, "")
    .trim();
  return {
    index: Number(open[1]),
    total: open[2] ? Number(open[2]) : null,
    body,
  };
}

export function loopItem(wrapped: string): ChatItem {
  const parsed = parseLoopIteration(wrapped);
  if (!parsed) {
    // Wrapper missing — render as a plain info line so the user
    // still sees that a loop iteration is queued.
    return { kind: "info", id: nid(), text: `↻ loop: ${wrapped}` };
  }
  return {
    kind: "loop",
    id: nid(),
    index: parsed.index,
    total: parsed.total,
    body: parsed.body,
    raw: wrapped,
  };
}

export function compactItem(status: string, summary: string): ChatItem {
  return { kind: "compact", id: nid(), status, summary };
}

export function infoItem(text: string): ChatItem {
  return { kind: "info", id: nid(), text };
}

export function errorItem(text: string): ChatItem {
  return { kind: "error", id: nid(), text };
}

/** Format a stats item into its display string. Pulled out of the
 *  renderer so unit tests can pin the format.
 *  ``think`` + ``out`` are estimated from the rendered text — what the
 *  user can see — not from Agno's billing metrics. The full billed
 *  total still shows in the tooltip (see ChatItems.tsx). */
export function formatStats(item: Extract<ChatItem, { kind: "stats" }>): string {
  const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const dur =
    item.duration >= 60
      ? `${Math.floor(item.duration / 60)}m ${Math.round(item.duration % 60)}s`
      : item.duration > 0
        ? `${item.duration.toFixed(1)}s`
        : "";
  const parts = [`✦ ${fmt(item.inputTokens)} in`];
  if (item.visibleThinkTokens > 0) parts.push(`${fmt(item.visibleThinkTokens)} think`);
  parts.push(`${fmt(item.visibleOutTokens)} out`);
  if (dur) parts.push(dur);
  return parts.join(" · ");
}

/** Rough chars→tokens estimate. Tiktoken in the browser would be more
 *  precise but adds ~250KB to the bundle for one display string;
 *  chars/4 is close enough for "this is what your reply costs"
 *  signalling. Code/markdown skews a little high but well within
 *  the ballpark a user can verify by eye. */
function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.max(1, Math.round(text.length / 4));
}

/** Walk back through items to the previous user message and collect
 *  every "thinking" / "assistant" item produced since then — that's
 *  the model's output for this single turn. Used to size the visible
 *  think/out tokens in the stats line. */
function collectVisibleSinceLastUser(
  items: ChatItem[],
): { thinking: string; assistant: string } {
  let thinking = "";
  let assistant = "";
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "user") break;
    if (it.kind === "thinking") thinking = it.text + " " + thinking;
    else if (it.kind === "assistant") assistant = it.text + " " + assistant;
  }
  return { thinking: thinking.trim(), assistant: assistant.trim() };
}

/** Same as ``collectVisibleSinceLastUser`` but scoped to a specific
 *  run: walks forward from a known user index until the next user
 *  item. Needed when ``run_completed`` fires late and a newer turn's
 *  items are already in the list — ``collectVisibleSinceLastUser``
 *  would conflate them with the completing run. */
function collectVisibleForRun(
  items: ChatItem[],
  userIdx: number,
): { thinking: string; assistant: string } {
  if (userIdx < 0) return collectVisibleSinceLastUser(items);
  let thinking = "";
  let assistant = "";
  for (let i = userIdx + 1; i < items.length; i++) {
    const it = items[i];
    if (it.kind === "user") break;
    if (it.kind === "thinking") thinking += it.text + " ";
    else if (it.kind === "assistant") assistant += it.text + " ";
  }
  return { thinking: thinking.trim(), assistant: assistant.trim() };
}

/** Replace the stats item matching ``runId`` with a corrected input
 *  count. Used after App.tsx fetches the real ``count_context_tokens``
 *  in response to a run completing. */
export function correctStatsCtx(
  items: ChatItem[],
  runId: string,
  contextTokens: number,
): ChatItem[] {
  return items.map((it) =>
    it.kind === "stats" && it.runId === runId
      ? { ...it, inputTokens: contextTokens, corrected: true }
      : it,
  );
}

export function assistantItem(text: string): ChatItem {
  return { kind: "assistant", id: nid(), text };
}

const SYSTEM_CONTEXT_RE = /<system-context>[\s\S]*?<\/system-context>\s*/g;
const ATTACHED_FILES_RE = /<attached-files>[\s\S]*?<\/attached-files>\s*/g;
// "@/abs/path" or "@./rel/path" or "@filename" — the BE @-mention shape.
const AT_PATH_RE = /(?:^|\s)@(\S+)/g;
// The BE wraps file refs in a hint line we can lift the paths out of.
const REFS_LINE_RE = /\[Referenced files:\s*([^\]]+?)\s+—\s+read before responding\]/;
// Closed think blocks, plus a trailing unclosed one (cancelled runs
// can persist mid-thought).
const THINK_BLOCK_RE = /<think>[\s\S]*?(<\/think>\s*|$)/g;

/** Build a stats ChatItem from a persisted-history ``stats`` turn.
 *  Backend ``get_chat_history`` emits one synthetic ``stats`` turn
 *  per completed top-level run (input_tokens, output_tokens,
 *  reasoning_tokens, duration). ``visibleOutText`` is the
 *  concatenated assistant content this run produced — used to
 *  estimate the ``out`` chars→tokens number the live path computes
 *  from the rendered DOM. Thinking content is stripped on restore
 *  so ``visibleThinkTokens`` is always 0 here. */
export function restoredStatsItem(
  turn: Record<string, unknown>,
  visibleOutText: string,
): ChatItem {
  const num = (k: string) => Number(turn[k] ?? 0) || 0;
  return {
    kind: "stats",
    id: nid(),
    runId: String(turn.run_id ?? ""),
    inputTokens: num("input_tokens"),
    outputTokens: num("output_tokens"),
    reasoningTokens: num("reasoning_tokens"),
    visibleThinkTokens: 0,
    visibleOutTokens: estimateTokens(visibleOutText),
    duration: num("duration"),
    // Historical stats land already-corrected — there's no in-flight
    // count_context_tokens RPC to overwrite them.
    corrected: true,
  };
}

/** Persisted-history turn → renderable item (TUI parity: strip the
 * injected system-context from user turns and think blocks from
 * assistant turns). Returns null for empty or non-chat turns.
 *
 * The full turn dict is passed (not just role + content) so the
 * mapper can pick up tool metadata (``tool_name``, ``args``,
 * ``is_error``) without a second signature for tool turns. */
export function restoredItem(turn: Record<string, unknown>): ChatItem | null {
  const role = String(turn.role ?? "");
  const content = String(turn.content ?? "");
  if (role === "user") {
    const text = content
      .replace(SYSTEM_CONTEXT_RE, "")
      .replace(ATTACHED_FILES_RE, "")
      .trim();
    if (!text) return null;
    // /loop iterations are stored on the BE as full wrapped user
    // messages (XML + autonomous-mode instructions + the user's
    // original ask). On reload, render them as the same tidy
    // iteration card the live path uses — otherwise the chat shows
    // the raw <loop-iteration> wrapper every time the user revisits
    // the session.
    if (text.startsWith("<loop-iteration ") || text.startsWith("<loop-iteration\n")) {
      return loopItem(text);
    }
    return userItem(text);
  }
  if (role === "assistant") {
    const text = content.replace(THINK_BLOCK_RE, "").trim();
    return text ? assistantItem(text) : null;
  }
  if (role === "thinking") {
    // Backend emits a synthetic ``thinking`` turn from the assistant
    // message's ``reasoning_content``. Reuses the live ``thinking``
    // kind so styling matches the streaming bubble.
    const text = content.trim();
    return text ? { kind: "thinking", id: nid(), text } : null;
  }
  if (role === "tool") {
    // Restore the tool card with status="done" — the live path
    // builds this in two events (``tool_started`` + ``tool_completed``).
    // History squashes the two; we land already-complete.
    const friendlyName = String(turn.friendly_name ?? turn.tool_name ?? "");
    const args = String(turn.args ?? "");
    const result = content;
    const isError = Boolean(turn.is_error);
    const runId = String(turn.run_id ?? "");
    return {
      kind: "tool",
      id: nid(),
      runId,
      name: friendlyName,
      args,
      status: isError ? "error" : "done",
      result,
      isError,
      // No diff rows in restored tool cards — the BE only computes
      // these on the live path via ``edit_file``'s rich payload.
      // A future enhancement could re-derive them by stashing the
      // diff in tool_args; today the textual result is enough.
      diffRows: null,
    };
  }
  if (role === "plan") {
    // Synthetic plan turn — emitted by ``get_chat_history`` in place
    // of an ``exit_plan_mode`` tool result. Lets the PlanCard render
    // at the point in the conversation where the agent submitted it,
    // not bolted onto the end.
    const planText = String(turn.plan ?? "").trim();
    if (!planText) return null;
    const runId = String(turn.run_id ?? "");
    const item = planItem(planText, normalizePlanTasks(turn.tasks), runId);
    if (item.kind === "plan") {
      const state = String(turn.state ?? "");
      if (state === "pending" || state === "approved" || state === "dismissed") {
        item.state = state;
      }
    }
    return item;
  }
  if (role === "visualization") {
    // Synthetic turn injected by ``get_chat_history`` from
    // ``session.visualizations`` — restores the json-render card
    // inline at the run it belonged to. The spec was already
    // saved by the FE via ``save_visualization`` after the
    // visualizer sub-agent completed.
    const spec = turn.spec;
    const specId = String(turn.spec_id ?? "");
    if (!specId || !spec || typeof spec !== "object") return null;
    return visualizationItem(
      spec,
      String(turn.title ?? ""),
      String(turn.source_agent ?? "visualizer"),
      specId,
    );
  }
  return null;
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
      // Track when the latest visible chunk landed so the user item
      // can stamp ``streamingEndedAt`` from this (rather than from
      // ``streaming_done``, which can lag by hundreds of ms).
      if (!msg.is_thinking) {
        stream.lastContentAt = Date.now();
      }
      // Agno-level reasoning events are already tagged; inline
      // <think> tags need the streaming parser.
      if (msg.is_thinking) {
        // Some providers emit reasoning_content that itself contains
        // literal "<think>" / "</think>" tags (MiniMax in particular
        // mixes the two conventions). Strip them defensively — the
        // thinking bubble is the wrapper, the tags are redundant noise.
        const cleaned = msg.text.replace(/<\/?think>/g, "");
        return appendText(items, cleaned, true);
      }
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
          // Attribute the tool to its agent — looked up via the
          // StreamState's run_id→agent map. Empty when this is a
          // top-level main-team tool call (no badge wanted there).
          agentName: stream.runToAgent.get(msg.run_id),
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
      // Stash the run_id → agent mapping so tool_started can stamp
      // its tool card with the owning agent's name. Both top-level
      // and sub-agent runs are recorded.
      if (msg.run_id && msg.agent_name) {
        stream.runToAgent.set(msg.run_id, msg.agent_name);
      }
      // Sub-agent dispatch marker (the main run has no parent).
      // Sub-agents that run under spawn_team / spawn_agent get a
      // structured ``agent_started`` orchestrate event that puts
      // them inside the team-progress card — so the inline
      // ``→ AgentName`` marker is only useful when NO orchestrate
      // card is live (rare).
      if (msg.parent_run_id) {
        const last = items[items.length - 1];
        if (last && last.kind === "orchestrate" && last.streaming) {
          // Already represented in the team-progress card — drop
          // the redundant inline marker.
          return items;
        }
        return [...items, { kind: "agent", id: nid(), text: `→ ${msg.agent_name}` }];
      }
      // Top-level run: stamp the run_id onto the most recent user
      // message that doesn't have one yet. The user item is added
      // optimistically by the FE when the message is submitted, then
      // this event lands once Agno assigns the run_id. Also record
      // the wall-clock start so we can surface user-perceived latency
      // (run_started → streaming_done) on the stats line, separate
      // from Agno's whole-run duration.
      if (msg.run_id) {
        const runId = msg.run_id;
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (it.kind === "user" && !it.runId) {
            return [
              ...items.slice(0, i),
              { ...it, runId, startedAt: Date.now() },
              ...items.slice(i + 1),
            ];
          }
          if (it.kind === "user") break; // already-tagged user → stop walking
        }
      }
      return items;

    case "streaming_done": {
      // Stash the wall-clock when the visible stream ended on the
      // matching user item. Prefer the timestamp of the *last
      // content chunk* over ``Date.now()`` here — ``streaming_done``
      // can lag the final delta by hundreds of ms on slow networks,
      // which would show up as bogus "extra time" on the stats line.
      if (!msg.run_id) return items;
      const runId = msg.run_id;
      const endedAt = stream.lastContentAt ?? Date.now();
      // Reset so the next run starts with a clean slate (and we fall
      // back to ``Date.now()`` if it streams nothing).
      stream.lastContentAt = undefined;
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === "user" && it.runId === runId) {
          return [
            ...items.slice(0, i),
            { ...it, streamingEndedAt: endedAt },
            ...items.slice(i + 1),
          ];
        }
      }
      return items;
    }

    case "model_completed":
      // Per-LLM-step token events are intentionally NOT rendered:
      // every step re-sends the whole context, so a multi-step run
      // printed several inflated-looking "N in" lines. The single
      // run_completed roll-up below replaces them.
      return items;

    case "run_completed": {
      // Top-level run finished — flip the most recent orchestrate
      // card's ``streaming`` flag off so the collapsed header drops
      // its spinner. Cheap: short walk from the end, only fires when
      // a card is actually present.
      if (!msg.parent_run_id) {
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (it.kind === "orchestrate" && it.streaming) {
            items = [
              ...items.slice(0, i),
              { ...it, streaming: false },
              ...items.slice(i + 1),
            ];
            break;
          }
          if (it.kind === "user") break; // older turn — leave its card alone
        }
      }
      // One stats line per top-level response. We count tokens from
      // what the user actually sees on screen (thinking blocks +
      // assistant reply since the last user turn) rather than Agno's
      // billed metrics, which sum across model iterations and include
      // invisible scratch (tool-routing JSON, internal chain steps).
      // Agno's billing numbers are still kept on the item so the
      // tooltip can show them.
      if (msg.parent_run_id) return items;
      if (!msg.input_tokens && !msg.output_tokens) return items;
      // Locate the user item that owns this run. We need it for two
      // reasons: (a) to compute the user-perceived duration, (b) to
      // insert the stats line at the right position — right BEFORE the
      // next user turn, not at the bottom of the list. Agno's
      // post-stream tail can run for 5-15s after the visible answer is
      // done, so on a fast reply followed by a fast follow-up the
      // stats line for run-N would otherwise land *after* the start
      // of run-(N+1)'s reply.
      let userIdx = -1;
      let displayDuration = msg.duration;
      if (msg.run_id) {
        const runId = msg.run_id;
        for (let i = items.length - 1; i >= 0; i--) {
          const it = items[i];
          if (it.kind === "user" && it.runId === runId) {
            userIdx = i;
            if (it.startedAt && it.streamingEndedAt) {
              displayDuration = (it.streamingEndedAt - it.startedAt) / 1000;
            }
            break;
          }
        }
      }
      // Token counts: walk just the slice that belongs to this turn
      // (the items between this run's user message and the next one,
      // or end of list) rather than "everything since last user".
      const turn = collectVisibleForRun(items, userIdx);
      const stats: ChatItem = {
        kind: "stats",
        id: nid(),
        runId: msg.run_id || "",
        inputTokens: msg.input_tokens,
        outputTokens: msg.output_tokens,
        reasoningTokens: msg.reasoning_tokens || 0,
        visibleThinkTokens: estimateTokens(turn.thinking),
        visibleOutTokens: estimateTokens(turn.assistant),
        duration: displayDuration,
        corrected: false,
      };
      // Where to insert? Right before the next user item after
      // ``userIdx`` — that's the logical "end of this turn". Fall back
      // to appending if we couldn't locate the run's user item.
      if (userIdx < 0) return [...items, stats];
      let insertAt = items.length;
      for (let i = userIdx + 1; i < items.length; i++) {
        if (items[i].kind === "user") {
          insertAt = i;
          break;
        }
      }
      return [...items.slice(0, insertAt), stats, ...items.slice(insertAt)];
    }

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
