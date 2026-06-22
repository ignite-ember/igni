/**
 * UI sandbox for the whole chat surface.
 *
 * Reachable at ``?demo=team`` (see main.tsx). Renders the same
 * components the live chat would, but driven by hardcoded mock data
 * instead of a running BE. Lets us iterate on the visual without
 * rebuilding the JetBrains plugin or running through a real
 * ``broadcast`` for every tweak.
 *
 * Two scenario kinds:
 *   - ``orchestrate`` — one team-progress card built from a
 *     stream of OrchestrateEvents (the original use case).
 *   - ``stream`` — a flat list of ChatItems rendered top-to-bottom
 *     the way the live chat renders the conversation. Use this for
 *     showcasing tool cards, edit diffs, thinking blocks, shell
 *     output, errors, compact summaries, loop markers, stats, etc.
 *
 * Editing this file: add new scenarios to ``SCENARIOS``. Each one
 * names a kind and either provides events (orchestrate) or items
 * (stream). Order: orchestrate scenarios first so the link stays
 * visually focused on the original purpose; stream scenarios after.
 */

import { useMemo, useState } from "react";
import {
  applyOrchestrateEvent,
  type ChatItem,
  type OrchestrateEvent,
} from "../chat/model";
import { ChatItemView } from "../components/ChatItems";
import { HitlDialog } from "../components/HitlDialog";
import type { DiffRow, HITLRequest } from "../protocol/messages";

type OrchestrateScenario = {
  id: string;
  title: string;
  description: string;
  kind: "orchestrate";
  events: OrchestrateEvent[];
};

type StreamScenario = {
  id: string;
  title: string;
  description: string;
  kind: "stream";
  items: ChatItem[];
  /** Optional HITL requirements rendered as the live ``HitlDialog``
   *  beneath the chat items — same component the real composer slots
   *  in when the BE pauses a run for approval. */
  hitl?: HITLRequest[];
};

type Scenario = OrchestrateScenario | StreamScenario;

/** Build a flood of tool events for a single agent — used by the
 *  ``wall-of-tools`` scenario so we can verify the body-scroll
 *  behaviour without standing up a real broadcast. */
function floodTools(agent: string, count: number): OrchestrateEvent[] {
  const out: OrchestrateEvent[] = [];
  for (let i = 0; i < count; i++) {
    const id = `flood-${agent}-${i}`;
    out.push({
      type: "tool_started",
      agent_path: agent,
      tool: i % 3 === 0 ? "rg" : i % 3 === 1 ? "read_file" : "run_shell_command",
      tool_call_id: id,
      args:
        i % 3 === 0
          ? `{pattern: 'TODO|FIXME', path: 'src/module_${i}.py'}`
          : i % 3 === 1
            ? `{path: 'src/module_${i}.py'}`
            : `['cat', 'src/module_${i}.py']`,
    });
    out.push({
      type: "tool_completed",
      agent_path: agent,
      tool: i % 3 === 0 ? "rg" : i % 3 === 1 ? "read_file" : "run_shell_command",
      tool_call_id: id,
      result: `Result line for tool ${i} — would be longer in reality.`,
      is_error: i % 11 === 0,
    });
  }
  return out;
}

/** Helper: monotonically-increasing fake ids so React keys stay stable
 *  within a scenario (each scenario calls this fresh). */
function idFactory() {
  let n = 1000;
  return () => ++n;
}

/** Build a DiffRow array from old/new line lists, matching the
 *  ``[text, style]`` shape the BE emits (see
 *  ``protocol/agno_events.py::_format_edit_diff``). The FE's
 *  ``DiffTable`` only inspects the leading char (+/-/space) for the
 *  row class, so the style string is unused in the demo. */
function makeDiff(
  startLine: number,
  before: string[],
  after: string[],
  /** Indices into ``before``/``after`` that are CHANGED. Lines outside
   *  the change ranges render as context (no +/-). */
  delIdx: number[],
  addIdx: number[],
): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNum = startLine;
  let newNum = startLine;

  // Walk before/after in parallel; for the demo we keep it simple and
  // emit context lines from ``after`` for indices that aren't in the
  // del/add sets. This is good enough for visual checks; the real BE
  // uses difflib.
  const delSet = new Set(delIdx);
  const addSet = new Set(addIdx);

  // Leading context (lines before any change).
  for (let i = 0; i < before.length; i++) {
    if (delSet.has(i)) {
      rows.push([`- ${String(oldNum).padStart(4)}   ${before[i]}`, "del"]);
      oldNum++;
    } else if (!addSet.has(i) && i < after.length) {
      rows.push([`  ${String(newNum).padStart(4)}   ${after[i]}`, ""]);
      oldNum++;
      newNum++;
    }
  }
  for (let i = 0; i < after.length; i++) {
    if (addSet.has(i)) {
      rows.push([`+ ${String(newNum).padStart(4)}   ${after[i]}`, "add"]);
      newNum++;
    }
  }
  return rows;
}

const newId = idFactory();

const SCENARIOS: Scenario[] = [
  {
    id: "broadcast-running",
    title: "Broadcast — three specialists, mid-flight",
    description:
      "Common case: spawn_team(broadcast: security, qa, reviewer). Tools in flight, some done.",
    kind: "orchestrate",
    events: [
      {
        type: "agent_started",
        agent_path: "security",
        agent: "security",
        parent: null,
        run_id: "run-security-1",
        task: "Audit how API keys and secrets are loaded and propagated through the codebase. Flag anything that looks risky.",
      },
      {
        type: "agent_started",
        agent_path: "qa",
        agent: "qa",
        parent: null,
        run_id: "run-qa-1",
        task: "Look at test coverage for the auth subsystem. Identify which scenarios are not exercised.",
      },
      {
        type: "agent_started",
        agent_path: "reviewer",
        agent: "reviewer",
        parent: null,
        run_id: "run-reviewer-1",
        task: "Scan the source tree for TODOs, FIXMEs, and obvious code smells. Suggest priorities.",
      },
      {
        type: "tool_started",
        agent_path: "security",
        tool: "run_shell_command",
        tool_call_id: "tc-sec-1",
        args: "['rg', '-n', 'api_key|secret', 'src/']",
      },
      {
        type: "tool_completed",
        agent_path: "security",
        tool: "run_shell_command",
        tool_call_id: "tc-sec-1",
        result:
          "src/auth/keys.py:12: api_key = os.getenv('OPENAI_API_KEY')\nsrc/auth/keys.py:25: secret = request.headers.get('X-Secret')\nsrc/config.py:8: api_key = config.get('api_key')",
        is_error: false,
      },
      {
        type: "tool_started",
        agent_path: "security",
        tool: "read_file",
        tool_call_id: "tc-sec-2",
        args: "{path: 'src/auth/keys.py'}",
      },
      {
        // The BE accumulates streamed deltas and emits the FULL
        // window every ~500ms — multi-line ``text`` joined by ``\n``.
        // The FE splits + replaces previewLines; that's why we ship
        // five lines in a single event here, not five separate ones.
        type: "content_preview",
        agent_path: "security",
        text: [
          "Looking at how API keys flow through the auth boundary…",
          "Confirmed: keys come from env, never logged.",
          "Checking the rate-limit middleware next.",
          "Hmm, no rate limit on /auth/refresh — flagging.",
          "Writing up the finding now — token reuse vector is real.",
        ].join("\n"),
      },
      {
        type: "tool_started",
        agent_path: "qa",
        tool: "run_shell_command",
        tool_call_id: "tc-qa-1",
        args: "['find', 'tests/', '-name', '*.py']",
      },
      {
        type: "tool_completed",
        agent_path: "qa",
        tool: "run_shell_command",
        tool_call_id: "tc-qa-1",
        result:
          "tests/test_auth.py\ntests/test_config.py\ntests/test_routes.py\ntests/conftest.py",
        is_error: false,
      },
      {
        type: "tool_started",
        agent_path: "qa",
        tool: "read_file",
        tool_call_id: "tc-qa-2",
        args: "{path: 'tests/test_auth.py'}",
      },
      {
        type: "content_preview",
        agent_path: "qa",
        text: [
          "Inventory: 4 test files in tests/.",
          "Checking coverage on the auth layer.",
          "Missing tests for: token rotation, refresh failure.",
          "Drafting test cases now — refresh failure first.",
        ].join("\n"),
      },
      {
        type: "tool_started",
        agent_path: "reviewer",
        tool: "rg",
        tool_call_id: "tc-rev-1",
        args: "{pattern: 'TODO|FIXME|XXX', path: 'src/'}",
      },
      {
        type: "tool_completed",
        agent_path: "reviewer",
        tool: "rg",
        tool_call_id: "tc-rev-1",
        result:
          "src/router.py:42: # TODO: rate-limit this endpoint\nsrc/queue.py:88: # FIXME: race on shutdown",
        is_error: false,
      },
      {
        type: "agent_completed",
        agent_path: "reviewer",
        is_error: false,
        input_tokens: 8420,
        output_tokens: 1240,
        reasoning_tokens: 380,
      },
    ],
  },
  {
    id: "one-error",
    title: "Broadcast — one specialist errored",
    description: "One sub-agent hit a tool error; others kept going.",
    kind: "orchestrate",
    events: [
      {
        type: "agent_started",
        agent_path: "security",
        agent: "security",
        parent: null,
        run_id: "run-security-1",
        task: "Audit how API keys and secrets are loaded and propagated through the codebase. Flag anything that looks risky.",
      },
      {
        type: "agent_started",
        agent_path: "perf",
        agent: "perf",
        parent: null,
        run_id: "run-perf-1",
        task: "Identify hot paths and look for unnecessary allocations or N+1 queries.",
      },
      {
        type: "tool_started",
        agent_path: "security",
        tool: "run_shell_command",
        tool_call_id: "e1",
        args: "['ls', '-la', '/missing-dir']",
      },
      {
        type: "tool_completed",
        agent_path: "security",
        tool: "run_shell_command",
        tool_call_id: "e1",
        result: "ls: /missing-dir: No such file or directory",
        is_error: true,
      },
      { type: "run_error", agent_path: "security", error: "Tool call failed after 1 attempt" },
      {
        type: "agent_completed",
        agent_path: "security",
        is_error: true,
        input_tokens: 3210,
        output_tokens: 410,
        reasoning_tokens: 120,
      },
      {
        type: "tool_started",
        agent_path: "perf",
        tool: "read_file",
        tool_call_id: "p1",
        args: "{path: 'src/hot_path.py'}",
      },
      {
        type: "content_preview",
        agent_path: "perf",
        text: "Scanning the hot path for allocations in the inner loop…",
      },
    ],
  },
  {
    id: "single-agent",
    title: "Single agent — no team",
    description: "spawn_agent(security) — one specialist, no broadcast.",
    kind: "orchestrate",
    events: [
      {
        type: "agent_started",
        agent_path: "security",
        agent: "security",
        parent: null,
        run_id: "run-security-1",
        task: "Audit how API keys and secrets are loaded and propagated through the codebase. Flag anything that looks risky.",
      },
      {
        type: "tool_started",
        agent_path: "security",
        tool: "rg",
        tool_call_id: "s1",
        args: "{pattern: 'eval|exec|compile', path: 'src/'}",
      },
      {
        type: "tool_completed",
        agent_path: "security",
        tool: "rg",
        tool_call_id: "s1",
        result: "(no matches found — good sign for code-injection risk)",
        is_error: false,
      },
      {
        type: "content_preview",
        agent_path: "security",
        text: "No dynamic-eval surface area in the source tree.",
      },
      {
        type: "agent_completed",
        agent_path: "security",
        is_error: false,
        input_tokens: 2150,
        output_tokens: 320,
      },
    ],
  },
  {
    id: "wall-of-tools",
    title: "One agent, 30 tool calls (scroll test)",
    description:
      "Exercises the per-agent body scroll. Each agent card should stay bounded; the tool list scrolls inside.",
    kind: "orchestrate",
    events: [
      { type: "agent_started", agent_path: "explorer", agent: "explorer", parent: null, run_id: "run-explorer-1" },
      {
        type: "content_preview",
        agent_path: "explorer",
        text: "Starting a wide sweep across the codebase.",
      },
      {
        type: "content_preview",
        agent_path: "explorer",
        text: "Reading every module to build a dependency map.",
      },
      {
        type: "content_preview",
        agent_path: "explorer",
        text: "30 files in, halfway through.",
      },
      ...floodTools("explorer", 30),
      {
        type: "content_preview",
        agent_path: "explorer",
        text: "Sweep finished — handing the map back to the parent.",
      },
    ],
  },
  {
    id: "streaming-window",
    title: "Streaming preview window — 5 lines, full sentences",
    description:
      "Reproduces what a healthy run looks like once content_preview semantics are right: the BE emits the FULL window each tick, the FE replaces previewLines. Each tick here is a separate event so you can see the window roll.",
    kind: "orchestrate",
    events: [
      { type: "agent_started", agent_path: "writer", agent: "writer", parent: null, run_id: "w1" },
      ...[
        ["Reading the spec to understand the contract."],
        [
          "Reading the spec to understand the contract.",
          "Found three endpoints — POST /jobs, GET /jobs/:id, DELETE /jobs/:id.",
        ],
        [
          "Reading the spec to understand the contract.",
          "Found three endpoints — POST /jobs, GET /jobs/:id, DELETE /jobs/:id.",
          "Checking the existing handler for shared idle-timeout logic.",
        ],
        [
          "Reading the spec to understand the contract.",
          "Found three endpoints — POST /jobs, GET /jobs/:id, DELETE /jobs/:id.",
          "Checking the existing handler for shared idle-timeout logic.",
          "Drafting the test cases now — happy path first.",
        ],
        [
          "Reading the spec to understand the contract.",
          "Found three endpoints — POST /jobs, GET /jobs/:id, DELETE /jobs/:id.",
          "Checking the existing handler for shared idle-timeout logic.",
          "Drafting the test cases now — happy path first.",
          "About to write the failing test for the cancel race.",
        ],
        [
          "Found three endpoints — POST /jobs, GET /jobs/:id, DELETE /jobs/:id.",
          "Checking the existing handler for shared idle-timeout logic.",
          "Drafting the test cases now — happy path first.",
          "About to write the failing test for the cancel race.",
          "Test is in — now wiring the handler to match.",
        ],
      ].map(
        (lines): OrchestrateEvent => ({
          type: "content_preview",
          agent_path: "writer",
          text: lines.join("\n"),
        }),
      ),
    ],
  },
  {
    id: "paused-hitl",
    title: "Sub-agent paused waiting for approval",
    description: "Specialist tried a destructive tool, gated for HITL approval.",
    kind: "orchestrate",
    events: [
      { type: "agent_started", agent_path: "refactor", agent: "refactor", parent: null, run_id: "run-refactor-1" },
      {
        type: "tool_started",
        agent_path: "refactor",
        tool: "edit_file",
        tool_call_id: "p1",
        args: "{path: 'src/critical.py', edits: [...]}",
      },
      { type: "agent_paused", agent_path: "refactor", count: 1 },
    ],
  },
  {
    id: "edit-tools",
    title: "Edit tools — replace, insert, multi-hunk, error",
    description:
      "Four variants of the edit tool card with realistic diff rows: a single-line replace, an insertion, a multi-line replacement, and an error (string-not-unique).",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "rename `getUser` to `loadUser` in `src/api/users.ts`",
        runId: "run-edit-1",
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-edit-1",
        name: "edit_file",
        args: "{file_path: 'src/api/users.ts', old_string: 'function getUser', new_string: 'function loadUser'}",
        status: "done",
        result: "",
        isError: false,
        diffRows: makeDiff(
          12,
          ["function getUser(id: string) {", "  return db.users.find(id);", "}"],
          ["function loadUser(id: string) {", "  return db.users.find(id);", "}"],
          [0],
          [0],
        ),
      },
      {
        kind: "assistant",
        id: newId(),
        text: "Renamed. Want me to update the call sites too?",
      },
      {
        kind: "user",
        id: newId(),
        text: "yes — also add a docstring above the new function",
        runId: "run-edit-2",
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-edit-2",
        name: "edit_file",
        args: "{file_path: 'src/api/users.ts', old_string: 'function loadUser', new_string: '/** Load a user by id. */\\nfunction loadUser'}",
        status: "done",
        result: "",
        isError: false,
        diffRows: [
          ["+ " + String(12).padStart(4) + "   /** Load a user by id. */", "add"],
          ["  " + String(13).padStart(4) + "   function loadUser(id: string) {", ""],
          ["  " + String(14).padStart(4) + "     return db.users.find(id);", ""],
          ["  " + String(15).padStart(4) + "   }", ""],
        ],
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-edit-2",
        name: "edit_file_replace_all",
        args: "{file_path: 'src/api/handlers.ts', old_string: 'getUser(', new_string: 'loadUser('}",
        status: "done",
        result: "Replaced 4 occurrences.",
        isError: false,
        diffRows: makeDiff(
          47,
          [
            "  const u = await getUser(req.params.id);",
            "  if (!u) return res.status(404).end();",
            "  const meta = await getUser(req.params.id, { withMeta: true });",
          ],
          [
            "  const u = await loadUser(req.params.id);",
            "  if (!u) return res.status(404).end();",
            "  const meta = await loadUser(req.params.id, { withMeta: true });",
          ],
          [0, 2],
          [0, 2],
        ),
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-edit-2",
        name: "edit_file",
        args: "{file_path: 'src/api/router.ts', old_string: 'route(', new_string: 'route2('}",
        status: "error",
        result:
          "Error: old_string appears 7 times in src/api/router.ts. Provide more surrounding context to make it unique, or use edit_file_replace_all.",
        isError: true,
        diffRows: null,
      },
      {
        kind: "assistant",
        id: newId(),
        text: "Call sites updated. The router rename failed — the substring isn't unique. Want me to use `edit_file_replace_all` there?",
      },
    ],
  },
  {
    id: "kitchen-sink",
    title: "Kitchen sink — every message kind",
    description:
      "A flat conversation that exercises every ChatItem renderer: user, assistant, thinking, tool (read/grep/bash), shell, info, error, compact, loop, stats.",
    kind: "stream",
    items: [
      // Loop iteration marker
      {
        kind: "loop",
        id: newId(),
        index: 1,
        total: 3,
        body: "scan the repo for stale TODOs and propose a triage",
        raw: "/loop\n\n<autonomous-loop>\nYou are inside iteration 1 of 3 of /loop. The user's original ask is below — work on it; the harness will re-invoke you for the next iteration.\n</autonomous-loop>\n\nscan the repo for stale TODOs and propose a triage",
      },
      {
        kind: "user",
        id: newId(),
        text: "where is the rate limit logic? feels like we set it twice somewhere",
        runId: "run-1",
      },
      {
        kind: "thinking",
        id: newId(),
        text: "The user thinks rate limits are defined in two places. Let me search for both decorator usage and middleware registration to confirm and pinpoint the duplication.",
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-1",
        name: "rg",
        args: "{pattern: 'rate_limit|RateLimit', path: 'src/'}",
        status: "done",
        result:
          "src/middleware/rate_limit.py:12:class RateLimitMiddleware:\nsrc/routes/auth.py:8:@rate_limit(per_minute=10)\nsrc/routes/auth.py:34:@rate_limit(per_minute=10)\nsrc/server.py:42:    app.add_middleware(RateLimitMiddleware, default_per_minute=60)",
        isError: false,
        diffRows: null,
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-1",
        name: "read_file",
        args: "{path: 'src/server.py', start_line: 30, end_line: 55}",
        status: "done",
        result:
          "def create_app() -> FastAPI:\n    app = FastAPI()\n    app.add_middleware(CORSMiddleware, allow_origins=['*'])\n    app.add_middleware(RateLimitMiddleware, default_per_minute=60)\n    app.include_router(auth_router)\n    return app",
        isError: false,
        diffRows: null,
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-1",
        name: "run_shell_command",
        args: "['wc', '-l', 'src/middleware/rate_limit.py']",
        status: "done",
        result: "  84 src/middleware/rate_limit.py",
        isError: false,
        diffRows: null,
      },
      {
        kind: "assistant",
        id: newId(),
        text: [
          "You're right — rate limiting is set up in **two places** and they fight:",
          "",
          "1. **Middleware** (`src/server.py:42`) — applies a 60/min default to every route.",
          "2. **Decorator** (`src/routes/auth.py:8,34`) — `@rate_limit(per_minute=10)` on the two auth endpoints.",
          "",
          "Both go through `RateLimitMiddleware`, but the decorator path checks its own counter, so an auth request gets rate-checked twice with different limits. The effective ceiling is the stricter of the two (10/min), but the looser counter still ticks — confusing in logs.",
          "",
          "Recommendation: keep the decorator (it lets us per-endpoint-tune) and have the middleware skip routes that already carry a `@rate_limit`.",
        ].join("\n"),
      },
      {
        kind: "stats",
        id: newId(),
        runId: "run-1",
        inputTokens: 12450,
        outputTokens: 980,
        reasoningTokens: 240,
        visibleThinkTokens: 60,
        visibleOutTokens: 220,
        duration: 8.4,
        corrected: true,
      },
      // /shell command
      {
        kind: "shell",
        id: newId(),
        command: "git status --short",
        output:
          " M src/middleware/rate_limit.py\n M src/server.py\n?? notes/rate-limit-fix.md",
        exitCode: 0,
      },
      // Shell command with non-zero exit
      {
        kind: "shell",
        id: newId(),
        command: "pytest tests/test_rate_limit.py",
        output:
          "============================= test session starts ==============================\ncollected 4 items\n\ntests/test_rate_limit.py::test_middleware_default PASSED                 [ 25%]\ntests/test_rate_limit.py::test_decorator_overrides FAILED                [ 50%]\ntests/test_rate_limit.py::test_double_counting PASSED                    [ 75%]\ntests/test_rate_limit.py::test_429_payload PASSED                        [100%]\n\nFAILED tests/test_rate_limit.py::test_decorator_overrides - AssertionError",
        exitCode: 1,
      },
      // Sub-agent dispatch breadcrumb
      {
        kind: "agent",
        id: newId(),
        text: "→ reviewer",
      },
      // Info banner
      {
        kind: "info",
        id: newId(),
        text: "Context auto-compacted — older messages summarized below.",
      },
      // Compact card
      {
        kind: "compact",
        id: newId(),
        status: "ok",
        summary: [
          "### Conversation so far",
          "",
          "- User asked why rate limiting felt doubled.",
          "- Found middleware (`server.py:42`) + decorator (`routes/auth.py`).",
          "- Confirmed both run; effective limit is the stricter of the two.",
          "- Recommended skipping middleware on routes carrying `@rate_limit`.",
        ].join("\n"),
      },
      // Error banner
      {
        kind: "error",
        id: newId(),
        text: "Backend disconnected — reconnecting…",
      },
    ],
  },
  {
    id: "tool-states",
    title: "Tool states — running, done, error",
    description:
      "All three tool-card statuses side by side so you can verify status dot colour, chevron, and error/result body.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "show me three tools at once please",
        runId: "run-states-1",
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-states-1",
        name: "run_shell_command",
        args: "['curl', '-sS', 'https://api.example.com/health']",
        status: "running",
        result: "",
        isError: false,
        diffRows: null,
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-states-1",
        name: "read_file",
        args: "{path: 'README.md'}",
        status: "done",
        result:
          "# Ember Code\n\nLocal-first AI coding agent. Backend in Python, web UI in React, …",
        isError: false,
        diffRows: null,
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-states-1",
        name: "run_shell_command",
        args: "['cat', '/etc/secret-that-doesnt-exist']",
        status: "error",
        result: "cat: /etc/secret-that-doesnt-exist: No such file or directory",
        isError: true,
        diffRows: null,
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-states-1",
        name: "rg",
        args: "{pattern: 'TODO', path: 'src/'}",
        status: "done",
        result:
          "src/cache.py:18: # TODO: invalidate on rename\nsrc/queue.py:88: # FIXME: race on shutdown",
        isError: false,
        diffRows: null,
        agentName: "reviewer",
      },
    ],
  },
  {
    id: "team-in-conversation",
    title: "Team — inside a real conversation",
    description:
      "What a team spawn actually looks like in chat: user asks a wide question, assistant says 'I'll fan out', the orchestrate card lands as a chat item with three specialists running, then the assistant synthesizes. Same orchestrate engine as the orchestrate-only scenarios — just rendered alongside the rest of the chat.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "do a full security pass on this repo — secrets, perms, dependency risk. parallel where you can.",
        runId: "run-team-1",
      },
      {
        kind: "assistant",
        id: newId(),
        text: "Spawning a three-specialist team — secrets/keys, filesystem perms, and dependency surface — running in parallel.",
      },
      buildOrchestrateItem([
        {
          type: "agent_started",
          agent_path: "secrets",
          agent: "secrets",
          parent: null,
          run_id: "run-secrets-1",
          task: "Find any plaintext secrets, API keys, or tokens that landed in the repo. Check .env files, config defaults, and test fixtures.",
        },
        {
          type: "agent_started",
          agent_path: "perms",
          agent: "perms",
          parent: null,
          run_id: "run-perms-1",
          task: "Audit filesystem permission boundaries — Unix socket modes, credentials.json chmod, any 0o644 file holding secrets.",
        },
        {
          type: "agent_started",
          agent_path: "deps",
          agent: "deps",
          parent: null,
          run_id: "run-deps-1",
          task: "Scan dependencies for known CVEs and unpinned versions. Flag anything coming from a sketchy origin.",
        },
        {
          type: "tool_started",
          agent_path: "secrets",
          tool: "rg",
          tool_call_id: "s-1",
          args: "{pattern: 'sk-[a-zA-Z0-9]{20,}|AKIA[0-9A-Z]{16}', path: '.'}",
        },
        {
          type: "tool_completed",
          agent_path: "secrets",
          tool: "rg",
          tool_call_id: "s-1",
          result:
            ".env:4: ANTHROPIC_API_KEY=sk-cp-cglyBn...redacted\ntests/fixtures/sample.env:2: OPENAI_API_KEY=sk-test-fake",
          is_error: false,
        },
        {
          type: "content_preview",
          agent_path: "secrets",
          text: [
            "Found a live key in .env at the repo root.",
            "Test fixture key is fake, ignoring.",
            "Checking if .env is gitignored…",
          ].join("\n"),
        },
        {
          type: "tool_started",
          agent_path: "perms",
          tool: "run_shell_command",
          tool_call_id: "p-1",
          args: "['stat', '-f', '%Sp %N', 'credentials.json', '.env']",
        },
        {
          type: "tool_completed",
          agent_path: "perms",
          tool: "run_shell_command",
          tool_call_id: "p-1",
          result:
            "-rw------- credentials.json\n-rw-r--r-- .env",
          is_error: false,
        },
        {
          type: "content_preview",
          agent_path: "perms",
          text: [
            "credentials.json correctly 0o600.",
            ".env is 0o644 — world-readable on multi-user macs.",
            "Drafting recommendation: chmod 0o600 on .env or move secrets to keychain.",
          ].join("\n"),
        },
        {
          type: "tool_started",
          agent_path: "deps",
          tool: "read_file",
          tool_call_id: "d-1",
          args: "{path: 'pyproject.toml'}",
        },
        {
          type: "tool_completed",
          agent_path: "deps",
          tool: "read_file",
          tool_call_id: "d-1",
          result:
            "[project]\ndependencies = [\n  \"agno~=2.4.0\",\n  \"httpx~=0.28.1\",\n  \"pydantic~=2.10.0\",\n  ...\n]",
          is_error: false,
        },
        {
          type: "agent_completed",
          agent_path: "deps",
          is_error: false,
          input_tokens: 6210,
          output_tokens: 720,
          reasoning_tokens: 180,
        },
      ]),
      {
        kind: "assistant",
        id: newId(),
        text: [
          "Synthesis from the three specialists:",
          "",
          "- **Critical**: live `ANTHROPIC_API_KEY` in `.env` at repo root. Rotate now.",
          "- **High**: `.env` is `0o644`. Either `chmod 0o600` it or move the key to the system keychain.",
          "- **Low**: dependency pins all use `~=` (allows patch but not minor) — supply-chain surface is acceptable.",
          "",
          "Want me to draft the keychain migration?",
        ].join("\n"),
      },
      {
        kind: "stats",
        id: newId(),
        runId: "run-team-1",
        inputTokens: 18900,
        outputTokens: 2150,
        reasoningTokens: 540,
        visibleThinkTokens: 0,
        visibleOutTokens: 520,
        duration: 14.7,
        corrected: true,
      },
    ],
  },
  {
    id: "hitl-single",
    title: "HITL — one approval needed",
    description:
      "The main agent wants to run a destructive shell command. The BE paused the run and emitted a hitl_request; the composer slot renders HitlDialog asking the user to confirm. Try the Allow/Reject buttons — they call the same handler the live flow does (here they just alert).",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "drop the local dev database and re-seed it from scratch",
        runId: "run-hitl-1",
      },
      {
        kind: "thinking",
        id: newId(),
        text: "This is destructive — the user did ask explicitly, but `rm` on the db file is gated by the permission system, so the BE will pause the run until I get approval.",
      },
      {
        kind: "tool",
        id: newId(),
        runId: "run-hitl-1",
        name: "run_shell_command",
        args: "['ls', '-la', '.local/db']",
        status: "done",
        result:
          "total 24\n-rw-r--r--  1 you  staff   8192 dev.db\n-rw-r--r--  1 you  staff   4096 dev.db-shm\n-rw-r--r--  1 you  staff   2048 dev.db-wal",
        isError: false,
        diffRows: null,
      },
      {
        kind: "info",
        id: newId(),
        text: "Run paused — waiting for your approval.",
      },
    ],
    hitl: [
      {
        type: "hitl_request",
        requirement_id: "req-1",
        tool_name: "run_shell_command",
        friendly_name: "run a shell command",
        tool_args: {
          command: ["rm", "-rf", ".local/db/"],
          cwd: "/Users/you/ai_coding/example-project",
        },
        details:
          "This command will delete files. Confirm so the agent can proceed.",
        agent_path: "main",
      },
    ],
  },
  {
    id: "hitl-batched",
    title: "HITL — three approvals batched",
    description:
      "The agent queued three actions in one tool-pass: shell delete, file edit, and an MCP write. The dialog walks you through them one at a time and submits the whole batch when the last is decided. Watch the 1/3 → 2/3 → 3/3 counter in the header.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "clean up the build dir, drop the stale config, and push the deploy.yaml change to staging",
        runId: "run-hitl-2",
      },
      {
        kind: "assistant",
        id: newId(),
        text: "I'll do all three in sequence. Each needs a separate confirmation.",
      },
      {
        kind: "info",
        id: newId(),
        text: "Run paused — 3 actions need approval.",
      },
    ],
    hitl: [
      {
        type: "hitl_request",
        requirement_id: "req-batch-1",
        tool_name: "run_shell_command",
        friendly_name: "run a shell command",
        tool_args: {
          command: ["rm", "-rf", "build/"],
          cwd: "/Users/you/ai_coding/example-project",
        },
        details: "Delete the build output directory before a fresh compile.",
        agent_path: "main",
      },
      {
        type: "hitl_request",
        requirement_id: "req-batch-2",
        tool_name: "edit_file",
        friendly_name: "edit a file",
        tool_args: {
          file_path: "/Users/you/ai_coding/example-project/config/staging.yaml",
          old_string: "replicas: 2",
          new_string: "replicas: 4",
        },
        details: "Bump staging replicas from 2 → 4 in config/staging.yaml.",
        agent_path: "main",
      },
      {
        type: "hitl_request",
        requirement_id: "req-batch-3",
        tool_name: "mcp_call",
        friendly_name: "call an MCP tool",
        tool_args: {
          server: "kubernetes",
          tool: "apply_manifest",
          args: {
            namespace: "staging",
            manifest_path: "config/staging.yaml",
          },
        },
        details:
          "Push deploy.yaml to the staging cluster via the kubernetes MCP server.",
        agent_path: "main",
      },
    ],
  },
  {
    id: "team-hitl",
    title: "Team — sub-agent paused for approval",
    description:
      "Three specialists in flight; the ``refactor`` sub-agent wants to edit a critical file and paused for approval. The team card shows that sub-agent in the ``paused`` state (status dot, no further work logged) and the HitlDialog appears below the conversation. Confirming would resume just that sub-agent; the other two keep running.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "audit auth, write the missing tests, and refactor the duplicated middleware. all in parallel if you can.",
        runId: "run-team-hitl",
      },
      {
        kind: "assistant",
        id: newId(),
        text: "Spawning auditor, tester, and refactor specialists in parallel.",
      },
      buildOrchestrateItem([
        {
          type: "agent_started",
          agent_path: "auditor",
          agent: "auditor",
          parent: null,
          run_id: "run-auditor-1",
          task: "Audit the auth subsystem for missing rate limits, token handling issues, and refresh-flow gaps.",
        },
        {
          type: "agent_started",
          agent_path: "tester",
          agent: "tester",
          parent: null,
          run_id: "run-tester-1",
          task: "Identify untested auth paths and write the missing tests.",
        },
        {
          type: "agent_started",
          agent_path: "refactor",
          agent: "refactor",
          parent: null,
          run_id: "run-refactor-1",
          task: "Collapse the duplicated middleware registration in src/server.py and the per-route decorator into a single source of truth.",
        },
        {
          type: "tool_started",
          agent_path: "auditor",
          tool: "rg",
          tool_call_id: "a-1",
          args: "{pattern: 'rate_limit|@auth_required', path: 'src/'}",
        },
        {
          type: "tool_completed",
          agent_path: "auditor",
          tool: "rg",
          tool_call_id: "a-1",
          result:
            "src/routes/auth.py:8:@rate_limit(per_minute=10)\nsrc/routes/auth.py:34:@rate_limit(per_minute=10)\nsrc/server.py:42:    app.add_middleware(RateLimitMiddleware, default_per_minute=60)",
          is_error: false,
        },
        {
          type: "content_preview",
          agent_path: "auditor",
          text:
            "Rate limit is set in two places. Drafting up the finding now — double counting on /auth/refresh.",
        },
        {
          type: "tool_started",
          agent_path: "tester",
          tool: "read_file",
          tool_call_id: "t-1",
          args: "{path: 'tests/test_auth.py'}",
        },
        {
          type: "content_preview",
          agent_path: "tester",
          text:
            "Inventory of existing tests done. Need to add: token rotation, refresh failure, concurrent refresh race.",
        },
        {
          type: "tool_started",
          agent_path: "refactor",
          tool: "edit_file",
          tool_call_id: "r-1",
          args: "{file_path: 'src/server.py', old_string: 'app.add_middleware(RateLimitMiddleware, default_per_minute=60)', new_string: 'app.add_middleware(SmartRateLimit)'}",
        },
        { type: "agent_paused", agent_path: "refactor", count: 1 },
      ]),
      {
        kind: "info",
        id: newId(),
        text: "Sub-agent ``refactor`` paused — waiting for your approval.",
      },
    ],
    hitl: [
      {
        type: "hitl_request",
        requirement_id: "req-team-1",
        tool_name: "edit_file",
        friendly_name: "edit a file",
        tool_args: {
          file_path: "/Users/you/ai_coding/example-project/src/server.py",
          old_string:
            "app.add_middleware(RateLimitMiddleware, default_per_minute=60)",
          new_string: "app.add_middleware(SmartRateLimit)",
        },
        details:
          "Refactor specialist wants to swap RateLimitMiddleware for SmartRateLimit at the FastAPI app-init site.",
        agent_path: "refactor",
      },
    ],
  },
  {
    id: "long-thinking",
    title: "Long thinking block",
    description:
      "A multi-paragraph reasoning trace — useful for checking the collapsible thinking block and its expanded layout.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "explain why our cache invalidation is wrong",
        runId: "run-thinking-1",
      },
      {
        kind: "thinking",
        id: newId(),
        text: [
          "Let me think through the cache invariants before I answer.",
          "",
          "The cache key is (user_id, doc_id). When a doc is renamed, doc_id stays the same — so the cache key is stable. That's correct.",
          "",
          "But when a doc is MOVED to a different user's library, doc_id stays the same and user_id changes. The cache entry for (old_user, doc_id) is now stale: the doc still exists but the old user no longer has access. We're not invalidating that entry on move.",
          "",
          "Also: on delete we invalidate (user_id, doc_id) — but if the doc was shared, other (other_user, doc_id) entries linger until TTL. So a recently-deleted doc can still be served from cache to a user who had a share.",
          "",
          "Two bugs. The move case is worse (data leak across users). The delete case is mostly an annoyance unless we treat cache hits as authorization.",
          "",
          "Plan: emit a `doc_access_changed` event on move/delete/unshare, and have the cache invalidate every (user, doc_id) entry where `user` is in the affected set.",
        ].join("\n"),
      },
      {
        kind: "assistant",
        id: newId(),
        text: [
          "Two distinct bugs:",
          "",
          "1. **Move across users isn't invalidated.** Cache key is `(user_id, doc_id)`. When a doc moves to a different user, the old `(old_user, doc_id)` entry stays warm — the previous owner keeps getting served the doc out of cache.",
          "2. **Shared-doc delete is partial.** Delete only invalidates `(deleter, doc_id)`. Other users who had a share continue reading from their cache until TTL.",
          "",
          "Fix: emit `doc_access_changed { affected_users: [...] }` on move/delete/unshare, and have the cache drop every entry where `user` is in that set. Cheaper than scanning the cache for `doc_id` matches and more correct than the per-user invalidation we have today.",
        ].join("\n"),
      },
    ],
  },
  {
    id: "markdown-rendering",
    title: "Markdown rendering — every block type",
    description:
      "An assistant answer that exercises every renderable markdown surface: inline code, four fenced-block language variants (bash, console, shellsession, text), a long code block to verify the collapse + expand chevron, blockquotes, lists, tables, headings, links. Useful when tweaking ``ChatItems.tsx`` markdown components or the highlight.js token theme.",
    kind: "stream",
    items: [
      {
        kind: "user",
        id: newId(),
        text: "show me every markdown block style you can render",
        runId: "run-md-1",
      },
      {
        kind: "assistant",
        id: newId(),
        text: [
          "# Heading 1 — markdown showcase",
          "",
          "Below is one assistant turn that exercises every block type the chat can render. Useful for visually regression-checking changes to ``ChatItems.tsx`` or the syntax-highlight theme.",
          "",
          "## Inline code",
          "",
          "Short references mid-sentence: run `npm ci`, then `cargo tauri dev` from `clients/tauri/`. Path-like references such as `src/components/Foo.tsx` and flag-like references such as `--no-deps` also use inline code.",
          "",
          "## Fenced ``bash`` — shell script source",
          "",
          "Keywords are highlighted but there's no `$` prompt. Use this for files saved as ``.sh``.",
          "",
          "```bash",
          "#!/usr/bin/env bash",
          "set -euo pipefail",
          "",
          "# Walk every TS file and report its line count.",
          "for f in src/**/*.ts; do",
          "  if [[ -f \"$f\" ]]; then",
          "    lines=$(wc -l < \"$f\")",
          "    echo \"$lines $f\"",
          "  fi",
          "done | sort -rn | head -20",
          "```",
          "",
          "## Fenced ``console`` — terminal session",
          "",
          "Lines starting with ``$`` render as prompts; everything after is treated as output. Use this when you want to show \"type this command, expect this output.\"",
          "",
          "```console",
          "$ npm install",
          "added 482 packages in 12s",
          "$ npm run dev",
          "",
          "> @ember-code/web-ui@0.1.0 dev",
          "> vite",
          "",
          "  VITE v6.4.3  ready in 129 ms",
          "  ➜  Local:   http://localhost:5179/",
          "```",
          "",
          "## Fenced ``shellsession`` — alias for console",
          "",
          "Same prompt styling as ``console``; both language hints exist for historical reasons.",
          "",
          "```shellsession",
          "$ git checkout -b feat/cool-thing",
          "Switched to a new branch 'feat/cool-thing'",
          "$ git status -s",
          " M README.md",
          "?? notes.txt",
          "```",
          "",
          "## Fenced ``text`` — plain monospace, no highlighting",
          "",
          "For ASCII diagrams, raw logs, or content you don't want the tokenizer touching.",
          "",
          "```text",
          "       ┌─────────────┐       ┌─────────────┐",
          "       │  Frontend   │──────▶│  Backend    │",
          "       │  (Tauri)    │  WS   │  (Python)   │",
          "       └─────────────┘       └─────────────┘",
          "             ▲                      │",
          "             │                      ▼",
          "             │              ┌─────────────┐",
          "             └──────────────│  Models     │",
          "                            └─────────────┘",
          "```",
          "",
          "## Long code block — verifies the collapse + expand chevron",
          "",
          "This Python file is over 50 lines, so the renderer clips it at ~220px and shows a chevron in the top-left + a fade overlay. Clicking the chevron OR anywhere on the clipped block expands it.",
          "",
          "```python",
          "from fastapi import FastAPI, HTTPException, Depends",
          "from pydantic import BaseModel",
          "from typing import Optional",
          "import uvicorn",
          "",
          "app = FastAPI(title=\"My API\", version=\"1.0.0\")",
          "",
          "# --- Models ---",
          "",
          "class Item(BaseModel):",
          "    name: str",
          "    description: Optional[str] = None",
          "    price: float",
          "    in_stock: bool = True",
          "",
          "class ItemResponse(BaseModel):",
          "    id: int",
          "    name: str",
          "    price: float",
          "",
          "# --- Fake DB ---",
          "",
          "items_db = {}",
          "next_id = 1",
          "",
          "# --- Dependency ---",
          "",
          "def get_current_user():",
          "    # Replace with actual auth logic",
          "    return {\"user_id\": 1, \"username\": \"demo\"}",
          "",
          "# --- Routes ---",
          "",
          "@app.get(\"/\")",
          "async def root():",
          "    return {\"message\": \"Hello, World!\"}",
          "",
          "@app.get(\"/items\", response_model=list[ItemResponse])",
          "async def list_items():",
          "    return list(items_db.values())",
          "",
          "@app.get(\"/items/{item_id}\", response_model=ItemResponse)",
          "async def get_item(item_id: int):",
          "    if item_id not in items_db:",
          "        raise HTTPException(status_code=404, detail=\"Item not found\")",
          "    return items_db[item_id]",
          "",
          "@app.post(\"/items\", response_model=ItemResponse, status_code=201)",
          "async def create_item(item: Item, user: dict = Depends(get_current_user)):",
          "    global next_id",
          "    new_item = ItemResponse(id=next_id, name=item.name, price=item.price)",
          "    items_db[next_id] = new_item",
          "    next_id += 1",
          "    return new_item",
          "",
          "if __name__ == \"__main__\":",
          "    uvicorn.run(app, host=\"0.0.0.0\", port=8000)",
          "```",
          "",
          "## Mermaid diagrams",
          "",
          "Fenced ``mermaid`` blocks render as SVG instead of code. The library is dynamically imported on first use so the ~700KB bundle only loads when a diagram actually appears.",
          "",
          "### Sequence diagram — startup handshake",
          "",
          "```mermaid",
          "sequenceDiagram",
          "  participant U as User",
          "  participant T as Tauri shell",
          "  participant P as Python BE",
          "  U->>T: launch app",
          "  T->>P: spawn (--ws-port 0)",
          "  P-->>T: ready { ws_port: 58129 }",
          "  T->>U: webview ?ws=ws://127.0.0.1:58129",
          "  U->>P: rpc list_sessions",
          "  P-->>U: [...]",
          "```",
          "",
          "### Flowchart — release pipeline",
          "",
          "```mermaid",
          "flowchart LR",
          "  tag[push v* tag] --> authorize{actor is<br/>admin?}",
          "  authorize -- yes --> ci[run ci.yml]",
          "  authorize -- no --> stop[abort]",
          "  ci --> pypi[publish PyPI]",
          "  pypi --> jb[build JetBrains]",
          "  pypi --> vsc[build VSCode]",
          "  pypi --> tauri[build Tauri matrix]",
          "  jb --> rel[github release]",
          "  vsc --> rel",
          "  tauri --> uj[updater latest.json]",
          "  uj --> rel",
          "```",
          "",
          "### State diagram — assistant turn lifecycle",
          "",
          "```mermaid",
          "stateDiagram-v2",
          "  [*] --> Idle",
          "  Idle --> Streaming: user submits",
          "  Streaming --> Streaming: content_delta",
          "  Streaming --> Tooling: tool_started",
          "  Tooling --> Streaming: tool_completed",
          "  Streaming --> Done: streaming_done",
          "  Done --> Idle: run_completed",
          "  Streaming --> Cancelled: user cancels",
          "  Cancelled --> Idle",
          "```",
          "",
          "### ER diagram — chat persistence",
          "",
          "```mermaid",
          "erDiagram",
          "  SESSION ||--o{ RUN : contains",
          "  RUN ||--o{ MESSAGE : has",
          "  RUN ||--|| METRICS : tracks",
          "  SESSION {",
          "    string session_id PK",
          "    string user_id",
          "    int updated_at",
          "  }",
          "  RUN {",
          "    string run_id PK",
          "    string session_id FK",
          "    string parent_run_id",
          "  }",
          "  MESSAGE {",
          "    string role",
          "    string content",
          "  }",
          "  METRICS {",
          "    int input_tokens",
          "    int output_tokens",
          "    float duration",
          "  }",
          "```",
          "",
          "## ASCII-art auto-rendering (svgbob)",
          "",
          "Plain fenced blocks whose content looks like ASCII art (Unicode box-drawing chars or repeated ``+--+`` corners) are routed to svgbob and rendered as SVG. The model can keep emitting its favorite ``┌─┐│▼──→`` shapes; the chat upgrades them visually. To see the conversion, each example below shows the **raw ASCII source first** (forced with the ``text`` language hint so it renders as plain code), then the **same source again with no language hint** so the auto-detect routes it through svgbob.",
          "",
          "### Example 1 — Unicode box-drawing",
          "",
          "**Source the model emitted:**",
          "",
          "```text",
          "┌──────┐    ┌──────┐    ┌────────────┐",
          "│ User │───▶│ Role │───▶│ Permission │",
          "└──────┘    └──────┘    └────────────┘",
          "```",
          "",
          "**What the chat shows after svgbob conversion:**",
          "",
          "```",
          "┌──────┐    ┌──────┐    ┌────────────┐",
          "│ User │───▶│ Role │───▶│ Permission │",
          "└──────┘    └──────┘    └────────────┘",
          "```",
          "",
          "### Example 2 — ASCII +/- corners",
          "",
          "**Source the model emitted:**",
          "",
          "```text",
          "+----------+      +-----------+",
          "|  Editor  |----->|  Viewer   |",
          "+----------+      +-----------+",
          "      |                  ^",
          "      v                  |",
          "+----------+             |",
          "|  Author  |-------------+",
          "+----------+",
          "```",
          "",
          "**What the chat shows after svgbob conversion:**",
          "",
          "```",
          "+----------+      +-----------+",
          "|  Editor  |----->|  Viewer   |",
          "+----------+      +-----------+",
          "      |                  ^",
          "      v                  |",
          "+----------+             |",
          "|  Author  |-------------+",
          "+----------+",
          "```",
          "",
          "### Explicit ``bob`` fence (rounded corners with ``.-``/``-`` / `` ` ``)",
          "",
          "If you want to force the svgbob renderer regardless of content shape, tag the fence with ``bob`` / ``svgbob`` / ``ascii``. svgbob also recognises rounded-corner syntax (``.--.`` for tops, `` `--' `` for bottoms) which renders as actual rounded SVG rectangles:",
          "",
          "**Source the model emitted (raw):**",
          "",
          "```text",
          "      .-----.       .-----.",
          "     | Cron  |---->| Job  |",
          "      `-----'       `-----'",
          "         |             |",
          "         v             v",
          "      .-----.       .-----.",
          "     | Log   |     |Result |",
          "      `-----'       `-----'",
          "```",
          "",
          "**Rendered:**",
          "",
          "```bob",
          "      .-----.       .-----.",
          "     | Cron  |---->| Job  |",
          "      `-----'       `-----'",
          "         |             |",
          "         v             v",
          "      .-----.       .-----.",
          "     | Log   |     |Result |",
          "      `-----'       `-----'",
          "```",
          "",
          "**How the conversion works:** the source is parsed by [svgbob](https://github.com/ivanceras/svgbob) (a WASM-compiled Rust crate, ~1.3MB, lazy-loaded). It detects line endpoints, corner shapes, arrowheads, and text labels, then emits a clean SVG with proper stroke geometry. Box-drawing chars (``┌─┐``), ASCII corners (``+-+``), arrows (``-->`` ``──▶`` ``v`` ``^``), rounded corners (``.- -.`` ``\\u0060-'``), and dashed lines (``- - -``) are all recognised. The detection lives in ``extractAsciiArtSource`` (``ChatItems.tsx``); anything that doesn't match falls through to plain code rendering.",
          "",
          "## Charts (Mermaid)",
          "",
          "Mermaid also renders quantitative chart types — no extra library needed.",
          "",
          "### Pie chart — token-budget breakdown",
          "",
          "```mermaid",
          "pie showData",
          "  title Tokens billed per turn (avg)",
          "  \"System prompt\" : 4200",
          "  \"Tool definitions\" : 5800",
          "  \"Conversation history\" : 6100",
          "  \"This turn user msg\" : 240",
          "  \"Visible assistant out\" : 1850",
          "```",
          "",
          "### XY chart — request latency",
          "",
          "```mermaid",
          "xychart-beta",
          "  title \"WS round-trip ms (last 7 days)\"",
          "  x-axis [Mon, Tue, Wed, Thu, Fri, Sat, Sun]",
          "  y-axis \"ms\" 0 --> 600",
          "  bar [220, 310, 280, 410, 380, 240, 260]",
          "  line [220, 310, 280, 410, 380, 240, 260]",
          "```",
          "",
          "### Gantt — release pipeline timeline",
          "",
          "```mermaid",
          "gantt",
          "  title Release v0.7.0",
          "  dateFormat YYYY-MM-DD",
          "  section Backend",
          "  Stats restoration       :done, be1, 2026-06-15, 3d",
          "  Editable install path   :done, be2, 2026-06-18, 1d",
          "  section Frontend",
          "  Code-block collapse     :done, fe1, 2026-06-19, 2d",
          "  Mermaid integration     :active, fe2, 2026-06-21, 1d",
          "  section Release",
          "  Apple Developer signing :crit, rel1, after fe2, 2d",
          "  Tag + ship              :rel2, after rel1, 1d",
          "```",
          "",
          "### Mindmap — Ember Code feature surface",
          "",
          "```mermaid",
          "mindmap",
          "  root((Ember Code))",
          "    Clients",
          "      Tauri desktop",
          "      VSCode extension",
          "      JetBrains plugin",
          "      Shared web UI",
          "    Backend",
          "      Agno runtime",
          "      MCP",
          "      Session pool",
          "      WS RPC",
          "    Capabilities",
          "      Code-aware chat",
          "      Multi-agent orchestrate",
          "      HITL approvals",
          "      Skills / slash cmds",
          "```",
          "",
          "### Timeline — Ember Code milestones",
          "",
          "```mermaid",
          "timeline",
          "  title Major milestones",
          "  v0.5.0 : First public Tauri build : Updater plugin wired",
          "  v0.6.0 : JetBrains + VSCode plugins : Zero-touch bootstrap",
          "  v0.6.4 : Signed updater : Minisign fallback",
          "  v0.7.0 : Apple Dev ID signing : Notarized DMG",
          "```",
          "",
          "### Quadrant chart — model picker tradeoffs",
          "",
          "```mermaid",
          "quadrantChart",
          "  title Model picker (quality vs cost)",
          "  x-axis Low cost --> High cost",
          "  y-axis Low quality --> High quality",
          "  quadrant-1 Premium picks",
          "  quadrant-2 Sweet spot",
          "  quadrant-3 Avoid",
          "  quadrant-4 Bargain bin",
          "  Claude Opus: [0.85, 0.95]",
          "  Claude Sonnet: [0.55, 0.85]",
          "  Claude Haiku: [0.20, 0.65]",
          "  MiniMax-M2.7: [0.10, 0.70]",
          "  GPT-4o-mini: [0.15, 0.55]",
          "```",
          "",
          "## Lists",
          "",
          "Unordered:",
          "",
          "- First item",
          "- Second item with a `code reference`",
          "- Third item",
          "  - Nested",
          "  - Also nested",
          "",
          "Ordered:",
          "",
          "1. Start the dev server with `cargo tauri dev`",
          "2. Open the app",
          "3. Hover the markdown demo button",
          "",
          "## Tables",
          "",
          "| Language | Use it for | Prompt styling |",
          "|----------|------------|----------------|",
          "| `bash` | shell script files | none |",
          "| `console` | terminal sessions | `$` highlighted |",
          "| `shellsession` | terminal sessions | `$` highlighted |",
          "| `text` | ASCII / raw output | none |",
          "",
          "## Blockquote",
          "",
          "> The fastest way to break the spell of someone else's framing is to draw the picture yourself.",
          "",
          "## Headings cascade",
          "",
          "### H3 — small section",
          "",
          "#### H4 — sub-section",
          "",
          "##### H5 — finer grain",
          "",
          "###### H6 — fine print",
          "",
          "## Emphasis",
          "",
          "*Italic*, **bold**, ***both***, ~~strikethrough~~, and a `mixed bold with code` (no, code wins).",
          "",
          "## Links",
          "",
          "External: [Tauri docs](https://v2.tauri.app/), [highlight.js languages](https://highlightjs.org/static/demo/).",
          "",
          "That's everything currently rendered by ``ChatItems.tsx``'s assistant pipeline.",
        ].join("\n"),
      },
      {
        kind: "stats",
        id: newId(),
        runId: "run-md-1",
        inputTokens: 8400,
        outputTokens: 1850,
        reasoningTokens: 0,
        visibleThinkTokens: 0,
        visibleOutTokens: 1820,
        duration: 12.6,
        corrected: true,
      },
    ],
  },
];

function buildOrchestrateItem(
  events: OrchestrateEvent[],
): Extract<ChatItem, { kind: "orchestrate" }> {
  let agents: Parameters<typeof applyOrchestrateEvent>[0] = {};
  let order: string[] = [];
  for (const ev of events) {
    const r = applyOrchestrateEvent(agents, order, ev);
    agents = r.agents;
    order = r.order;
  }
  return {
    kind: "orchestrate",
    id: 1,
    cardId: "demo",
    agents,
    order,
    streaming: events[events.length - 1]?.type !== "agent_completed",
  };
}

function renderItems(scenario: Scenario): ChatItem[] {
  if (scenario.kind === "orchestrate") {
    return [buildOrchestrateItem(scenario.events)];
  }
  return scenario.items;
}

function scenarioHitl(scenario: Scenario): HITLRequest[] | null {
  return scenario.kind === "stream" && scenario.hitl?.length
    ? scenario.hitl
    : null;
}

export function OrchestrateDemo() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[0].id);
  const scenario = useMemo(
    () => SCENARIOS.find((s) => s.id === scenarioId) || SCENARIOS[0],
    [scenarioId],
  );
  const items = useMemo(() => renderItems(scenario), [scenario]);
  const hitl = scenarioHitl(scenario);
  // ``HitlDialog`` mounts its own step counter — when the user
  // switches scenarios we need to remount so the counter resets.
  // Keying on scenario.id does that without leaking decisions
  // across scenarios.
  const hitlKey = scenario.id;

  return (
    <div className="demo-shell">
      <header className="demo-head">
        <h1>Ember Code UI sandbox</h1>
        <p>
          Mock ChatItems rendered through the real components. Pick a
          scenario below; tweak{" "}
          <code>clients/web/src/dev/OrchestrateDemo.tsx</code> to add
          more.
        </p>
        <nav className="demo-nav">
          {SCENARIOS.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`demo-nav-btn${s.id === scenarioId ? " active" : ""}`}
              onClick={() => setScenarioId(s.id)}
            >
              {s.title}
            </button>
          ))}
        </nav>
        <p className="demo-desc">{scenario.description}</p>
      </header>
      <main className="demo-stage">
        <div className="col">
          {items.map((item) => (
            <ChatItemView
              key={item.id}
              item={item}
              onEditUser={(_, newText) =>
                alert(`(demo) Would edit user message to:\n\n${newText}`)
              }
              onDeleteUser={() => alert("(demo) Would delete user message + truncate")}
              onStopTeam={() =>
                alert("(demo) Would cancel the whole team run via client.cancel()")
              }
              onStopAgent={(runId) =>
                alert(`(demo) Would cancel sub-agent run_id=${runId} via cancel_agent_run RPC`)
              }
              onRetryAgent={(name, task) =>
                alert(
                  `(demo) Would send follow-up user message:\n\n` +
                    `Retry the ${name} sub-agent with this prompt:\n\n${task}`,
                )
              }
            />
          ))}
          {hitl && (
            <div className="composer-hitl">
              <HitlDialog
                key={hitlKey}
                requirements={hitl}
                onResolve={(decisions) =>
                  alert(
                    "(demo) Would send hitl_response_batch:\n\n" +
                      JSON.stringify(decisions, null, 2),
                  )
                }
              />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
