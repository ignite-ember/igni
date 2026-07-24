import { describe, expect, it } from "vitest";
import {
  applyEvent,
  correctStatsCtx,
  extractAttachedPaths,
  formatStats,
  loopItem,
  mergePlanTasks,
  normalizePlanTask,
  normalizePlanTasks,
  parseLoopIteration,
  type PlanTask,
  restoredItem,
  restoredStatsItem,
  type ChatItem,
} from "./model";
import type { ServerMessage } from "../protocol/messages";

const delta = (text: string, is_thinking = false): ServerMessage =>
  ({ type: "content_delta", text, is_thinking }) as ServerMessage;

const toolStarted: ServerMessage = {
  type: "tool_started",
  tool_name: "read_file",
  friendly_name: "Read",
  args_summary: "settings.py",
  run_id: "r1",
} as ServerMessage;

const toolCompleted: ServerMessage = {
  type: "tool_completed",
  summary: "ok",
  full_result: "content",
  run_id: "r1",
  has_markup: false,
  diff_rows: null,
  is_error: false,
} as ServerMessage;

function play(events: ServerMessage[]): ChatItem[] {
  let items: ChatItem[] = [];
  for (const e of events) items = applyEvent(items, e);
  return items;
}

describe("applyEvent", () => {
  it("routes content_delta by is_thinking flag (BE does the parse)", () => {
    const items = play([delta("hidden", true), delta("the answer")]);
    expect(items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
    expect((items[0] as { text: string }).text).toBe("hidden");
    expect((items[1] as { text: string }).text).toBe("the answer");
  });

  it("BE pre-classifies chunks; the FE routes by is_thinking", () => {
    const items = play([
      delta("thinking chunk", true),
      delta("visible chunk with <think> literal", false),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
    expect((items[0] as { text: string }).text).toBe("thinking chunk");
    expect((items[1] as { text: string }).text).toBe("visible chunk with <think> literal");
  });

  it("merges consecutive same-kind deltas into one item", () => {
    const items = play([delta("Hello "), delta("world")]);
    expect(items).toHaveLength(1);
    expect((items[0] as { text: string }).text).toBe("Hello world");
  });

  it("respects the is_thinking flag from Agno reasoning events", () => {
    const items = play([delta("native reasoning", true), delta("answer")]);
    expect(items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
  });

  it("BE already split reasoning: deltas arrive pre-classified", () => {
    // The BE routes thinking-mode chunks as is_thinking=true and
    // visible-mode chunks as is_thinking=false across one run; the
    // FE just appends to the right bubble by is_thinking.
    const items = play([
      delta("before tool", false),
      toolStarted,
      delta("post tool", false),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["assistant", "tool", "assistant"]);
  });

  it("updates the running tool card on completion", () => {
    const items = play([toolStarted, toolCompleted]);
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.status).toBe("done");
    expect(tool.result).toBe("content");
  });

  it("marks failed tools as errors", () => {
    const items = play([
      toolStarted,
      { ...toolCompleted, is_error: true } as ServerMessage,
    ]);
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.status).toBe("error");
  });

  // ── tool_error event (separate from tool_completed{is_error}) ──
  // ``tool_error`` is the dedicated error event the BE emits when
  // a tool raises rather than returning a result. Walks back the
  // items list to find the most recent ``running`` tool card and
  // patches it with the error. If no running tool is found, falls
  // through to a standalone error item (defensive — tool_error
  // arriving with no prior tool_started shouldn't disappear).

  it("tool_error patches the running tool card with the error message", () => {
    const items = play([
      toolStarted,
      {
        type: "tool_error",
        error: "permission denied",
        run_id: "r1",
      } as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.kind).toBe("tool");
    expect(tool.status).toBe("error");
    expect(tool.result).toBe("permission denied");
    expect(tool.isError).toBe(true);
  });

  it("tool_error only patches a RUNNING tool (skips already-completed)", () => {
    // Defensive — if a stale tool_error arrives after the tool
    // completed normally, don't reopen the closed card. The
    // walk-back skips any non-running tool.
    const items = play([
      toolStarted,
      toolCompleted,
      {
        type: "tool_error",
        error: "ghost error",
        run_id: "r1",
      } as ServerMessage,
    ]);
    // The completed tool stays done; the error spawns a
    // standalone error item.
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.status).toBe("done");
    // And the error landed as a separate item.
    expect(items.at(-1)?.kind).toBe("error");
  });

  it("tool_error with no prior tool_started spawns a standalone error item", () => {
    // The fallback at the end of the case branch — don't
    // silently drop the error.
    const items = play([
      {
        type: "tool_error",
        error: "out-of-band failure",
        run_id: "x",
      } as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("error");
    expect((items[0] as { text: string }).text).toBe("out-of-band failure");
  });

  it("tool_error patches the MOST RECENT running tool (multiple cards)", () => {
    // Two concurrent tool cards both running. The error
    // should patch the most recent one (closest match
    // to whatever caused the error).
    const second: ServerMessage = {
      ...toolStarted,
      tool_name: "edit_file",
      friendly_name: "Edit",
      run_id: "r1",
    } as ServerMessage;
    const items = play([
      toolStarted,
      second,
      {
        type: "tool_error",
        error: "edit failed",
        run_id: "r1",
      } as ServerMessage,
    ]);
    const first = items[0] as Extract<ChatItem, { kind: "tool" }>;
    const last = items[1] as Extract<ChatItem, { kind: "tool" }>;
    // First tool still running.
    expect(first.status).toBe("running");
    // Second tool got the error.
    expect(last.status).toBe("error");
    expect(last.result).toBe("edit failed");
  });

  // ── run_error / error / info top-level events ─────────────

  it("run_error appends an error item", () => {
    const items = play([
      { type: "run_error", error: "model timed out" } as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("error");
    expect((items[0] as { text: string }).text).toBe("model timed out");
  });

  it("error event appends an error item", () => {
    // The plain ``error`` event (vs ``run_error`` which carries
    // a run_id) — same shape, same handling.
    const items = play([
      { type: "error", text: "connection lost" } as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("error");
    expect((items[0] as { text: string }).text).toBe("connection lost");
  });

  it("info event appends an info item", () => {
    // Info events surface BE-side notifications (e.g. "Knowledge
    // sync loaded 5 entries"). Distinct from error styling.
    const items = play([
      { type: "info", text: "knowledge sync complete" } as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("info");
    expect((items[0] as { text: string }).text).toBe("knowledge sync complete");
  });

  it("unknown event types fall through (default branch — no-op)", () => {
    // Forward-compat — if the BE adds a new event type the FE
    // doesn't recognise, items pass through unchanged rather
    // than crashing.
    const items = play([
      delta("existing content"),
      { type: "some_future_event", payload: { x: 1 } } as unknown as ServerMessage,
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("assistant");
  });
});

describe("restoredItem", () => {
  it("strips injected system-context from user turns", () => {
    const item = restoredItem({
      role: "user",
      content:
        "<system-context>Current datetime: 2026-06-11 14:42 CEST</system-context>\nCount to 20.",
    });
    expect(item).toMatchObject({ kind: "user", text: "Count to 20." });
  });

  it("strips closed think blocks from assistant turns", () => {
    const item = restoredItem({
      role: "assistant",
      content: "<think>plan it out</think>\nDone. Created notes.txt.",
    });
    expect(item).toMatchObject({ kind: "assistant", text: "Done. Created notes.txt." });
  });

  it("strips a trailing unclosed think block (cancelled run)", () => {
    const item = restoredItem({
      role: "assistant",
      content: "Partial answer.\n<think>was still reason",
    });
    expect(item).toMatchObject({ kind: "assistant", text: "Partial answer." });
  });

  it("renders a /loop iteration as a structured loop item, not a raw user bubble", () => {
    const wrapped =
      '<loop-iteration index="3" total="5">\n' +
      "Autonomous loop iteration — do not ask the user; perform one unit of work and stop. " +
      "Tool-permission prompts are the only legitimate user interaction.\n\n" +
      "When you can determine the total number of items (e.g. after listing files or parsing the input), " +
      "call loop_set_total(N) once so the panel renders progress as N/total. " +
      "Call loop_stop() when all work is done — don't keep looping just because the safety cap hasn't been hit.\n\n" +
      "process file 3\n" +
      "</loop-iteration>";
    const item = restoredItem({ role: "user", content: wrapped });
    expect(item?.kind).toBe("loop");
    if (item?.kind === "loop") {
      expect(item.index).toBe(3);
      expect(item.total).toBe(5);
      expect(item.body).toBe("process file 3");
    }
  });

  it("returns null when nothing remains or role is unknown", () => {
    expect(restoredItem({ role: "assistant", content: "<think>only thoughts</think>" })).toBeNull();
    expect(restoredItem({ role: "user", content: "  " })).toBeNull();
    expect(restoredItem({ role: "system", content: "boot" })).toBeNull();
  });

  it("restores a thinking turn into a thinking ChatItem", () => {
    const item = restoredItem({
      role: "thinking",
      content: "let me reason through this carefully…",
    });
    expect(item).toMatchObject({
      kind: "thinking",
      text: "let me reason through this carefully…",
    });
  });

  it("returns null for an empty thinking turn", () => {
    expect(restoredItem({ role: "thinking", content: "   " })).toBeNull();
  });

  it("restores a tool turn into a done tool ChatItem", () => {
    const item = restoredItem({
      role: "tool",
      tool_name: "run_shell_command",
      friendly_name: "Bash",
      args: "command=ls -la",
      content: "drwxr-xr-x  6 user  staff   192 ...\n",
      is_error: false,
      run_id: "r1",
    });
    expect(item).toMatchObject({
      kind: "tool",
      name: "Bash",
      args: "command=ls -la",
      status: "done",
      isError: false,
      runId: "r1",
    });
    if (item?.kind === "tool") {
      expect(item.result).toContain("drwxr-xr-x");
    }
  });

  it("marks restored tool turns with tool_call_error as error", () => {
    const item = restoredItem({
      role: "tool",
      tool_name: "edit_file",
      friendly_name: "Edit",
      args: "file_path=missing.py",
      content: "Error: file not found",
      is_error: true,
      run_id: "r1",
    });
    expect(item).toMatchObject({ kind: "tool", status: "error", isError: true });
  });
});

describe("run stats line", () => {
  const modelCompleted = (input: number, output: number): ServerMessage =>
    ({ type: "model_completed", input_tokens: input, output_tokens: output, run_id: "r1", parent_run_id: "" }) as ServerMessage;
  const runCompleted = (
    input: number,
    output: number,
    duration: number,
    parent = "",
    reasoning = 0,
  ): ServerMessage =>
    ({
      type: "run_completed",
      input_tokens: input,
      output_tokens: output,
      reasoning_tokens: reasoning,
      duration,
      run_id: "r1",
      parent_run_id: parent,
    }) as ServerMessage;

  // Stats items render via formatStats — exercise that path here so
  // the tests pin the visible string the chat shows.
  const statsItem = (item: ChatItem) => {
    if (item.kind !== "stats") throw new Error("not a stats item: " + item.kind);
    return item;
  };
  const statsText = (item: ChatItem) => formatStats(statsItem(item));

  // 'out' / 'think' now count chars-of-rendered-text/4. Build helpers
  // that emit deltas of the right size so we can assert clean counts.
  const reply = (tokens: number) => delta("a".repeat(tokens * 4));      // visible
  const think = (tokens: number) => delta("a".repeat(tokens * 4), true); // reasoning
  const lastStats = (items: ChatItem[]) =>
    items.filter((i) => i.kind === "stats").at(-1) as ChatItem;

  it("does not render per-step model_completed badges", () => {
    expect(
      play([modelCompleted(14000, 90), modelCompleted(15000, 80)]).filter(
        (i) => i.kind === "stats",
      ),
    ).toHaveLength(0);
  });

  it("renders one roll-up line with duration on run_completed", () => {
    const items = play([reply(240), runCompleted(27400, 240, 12.4)]);
    expect(statsText(lastStats(items))).toBe("✦ 27.4k in · 240 out · 12.4s");
  });

  it("formats minute-scale durations and skips zero duration", () => {
    expect(
      statsText(lastStats(play([reply(1), runCompleted(1000, 1, 75)]))),
    ).toContain("1m 15s");
    expect(
      statsText(lastStats(play([reply(1), runCompleted(1000, 1, 0)]))),
    ).toBe("✦ 1.0k in · 1 out");
  });

  it("ignores sub-agent run_completed events", () => {
    expect(
      play([runCompleted(5000, 50, 3, "parent-run")]).filter(
        (i) => i.kind === "stats",
      ),
    ).toHaveLength(0);
  });

  it("splits visible thinking and visible reply into separate segments", () => {
    // Reasoning model: 280 tokens of thinking, 50 tokens of visible
    // reply. Agno billed it as 330 output total (280 reasoning) — we
    // don't read those for display; we count what we rendered.
    const items = play([think(280), reply(50), runCompleted(22000, 330, 16, "", 280)]);
    expect(statsText(lastStats(items))).toBe("✦ 22.0k in · 280 think · 50 out · 16.0s");
  });

  it("correctStatsCtx replaces input with count_context_tokens", () => {
    const items = play([think(280), reply(50), runCompleted(22000, 330, 16, "", 280)]);
    const fixed = correctStatsCtx(items, "r1", 13900);
    expect(statsText(lastStats(fixed))).toBe(
      "✦ 13.9k in · 280 think · 50 out · 16.0s",
    );
    // Other stats items (different runId) shouldn't move.
    const otherRun = correctStatsCtx(items, "r2", 9999);
    expect(statsText(lastStats(otherRun))).toBe(
      "✦ 22.0k in · 280 think · 50 out · 16.0s",
    );
  });
});

describe("parseLoopIteration", () => {
  const wrapped = (idx: number, total: number | null, body: string) =>
    `<loop-iteration index="${idx}"${total ? ` total="${total}"` : ""}>\n` +
    `Autonomous loop iteration — do not ask the user; perform one unit of work and stop. ` +
    `Tool-permission prompts are the only legitimate user interaction.\n\n` +
    `When you can determine the total number of items (e.g. after listing files or parsing the input), ` +
    `call loop_set_total(N) once so the panel renders progress as N/total. ` +
    `Call loop_stop() when all work is done — don't keep looping just because the safety cap hasn't been hit.\n\n` +
    `${body}\n` +
    `</loop-iteration>`;

  it("extracts iteration index, total, and the original ask", () => {
    const parsed = parseLoopIteration(
      wrapped(2, 45, "Through each president of the USA and sum all their dates of birth. Do it one by one"),
    );
    expect(parsed).toEqual({
      index: 2,
      total: 45,
      body:
        "Through each president of the USA and sum all their dates of birth. Do it one by one",
    });
  });

  it("returns total null when the BE omits the attribute", () => {
    const parsed = parseLoopIteration(wrapped(1, null, "do thing X"));
    expect(parsed?.total).toBeNull();
    expect(parsed?.body).toBe("do thing X");
  });

  it("preserves blank lines inside the user's prompt", () => {
    const body = "Step 1: list files\n\nStep 2: summarize each";
    const parsed = parseLoopIteration(wrapped(3, 10, body));
    expect(parsed?.body).toBe(body);
  });

  it("returns null when the wrapper is missing", () => {
    expect(parseLoopIteration("just plain text")).toBeNull();
  });

  it("loopItem builds a structured chat item; falls back to info on bad input", () => {
    const ok = loopItem(wrapped(7, 12, "the ask"));
    expect(ok.kind).toBe("loop");
    if (ok.kind === "loop") {
      expect(ok.index).toBe(7);
      expect(ok.total).toBe(12);
      expect(ok.body).toBe("the ask");
    }
    const bad = loopItem("not a loop wrapper");
    expect(bad.kind).toBe("info");
  });
});

// ── Plan-task normalization (CC parity row 50) ──────────────
//
// The agent calls ``exit_plan_mode(plan, tasks=[...])`` and a
// ``plan_submitted`` push lands. The shape from the BE is loose
// (dict[str, Any]) so we have to be defensive about every field
// — a malformed task must drop, not crash the chat. These tests
// pin the contract the App.tsx channel handler relies on.

describe("normalizePlanTask", () => {
  it("normalizes a fully-formed task", () => {
    const out = normalizePlanTask({
      content: "Run tests",
      status: "in_progress",
      activeForm: "Running tests",
    });
    expect(out).toEqual({
      content: "Run tests",
      status: "in_progress",
      activeForm: "Running tests",
    });
  });

  it("drops tasks with empty content (after trim)", () => {
    // Empty content is unusable — the merge step matches by
    // content, so an empty-string key would collide with every
    // other empty task in the list.
    expect(normalizePlanTask({ content: "", status: "pending" })).toBeNull();
    expect(normalizePlanTask({ content: "   ", status: "pending" })).toBeNull();
    expect(normalizePlanTask({ status: "pending" })).toBeNull();
  });

  it("drops non-object input", () => {
    expect(normalizePlanTask(null)).toBeNull();
    expect(normalizePlanTask(undefined)).toBeNull();
    expect(normalizePlanTask("string")).toBeNull();
    expect(normalizePlanTask(42)).toBeNull();
    expect(normalizePlanTask([])).toBeNull();
  });

  it("trims content + activeForm", () => {
    const out = normalizePlanTask({
      content: "  spaced  ",
      activeForm: "  Spacing  ",
    });
    expect(out?.content).toBe("spaced");
    expect(out?.activeForm).toBe("Spacing");
  });

  it("defaults status to 'pending' when missing", () => {
    expect(normalizePlanTask({ content: "x" })?.status).toBe("pending");
  });

  it("defaults status to 'pending' for unknown values", () => {
    // Forward-compat: if the BE ever ships a new status the FE
    // doesn't know, treat it as pending rather than rendering
    // a confused/broken card.
    expect(
      normalizePlanTask({ content: "x", status: "blocked" })?.status,
    ).toBe("pending");
    expect(normalizePlanTask({ content: "x", status: 7 })?.status).toBe(
      "pending",
    );
  });

  it("defaults missing activeForm to empty string", () => {
    // Empty ``activeForm`` is fine — the renderer falls back to
    // ``content`` when there's no gerund to show.
    expect(normalizePlanTask({ content: "x" })?.activeForm).toBe("");
  });
});

describe("normalizePlanTasks", () => {
  it("returns [] for non-array input", () => {
    expect(normalizePlanTasks(undefined)).toEqual([]);
    expect(normalizePlanTasks(null)).toEqual([]);
    expect(normalizePlanTasks({ tasks: [] })).toEqual([]);
    expect(normalizePlanTasks("not an array")).toEqual([]);
  });

  it("filters out unnormalizable entries silently", () => {
    const out = normalizePlanTasks([
      { content: "good", status: "pending" },
      null,
      { content: "" },
      "string",
      { content: "also good", status: "completed" },
    ]);
    expect(out).toHaveLength(2);
    expect(out[0].content).toBe("good");
    expect(out[1].content).toBe("also good");
  });

  it("preserves input order", () => {
    const out = normalizePlanTasks([
      { content: "a" },
      { content: "b" },
      { content: "c" },
    ]);
    expect(out.map((t) => t.content)).toEqual(["a", "b", "c"]);
  });
});

describe("mergePlanTasks", () => {
  const planTasks: PlanTask[] = [
    { content: "Read code", status: "completed", activeForm: "Reading code" },
    { content: "Run tests", status: "pending", activeForm: "Running tests" },
    { content: "Ship", status: "pending", activeForm: "Shipping" },
  ];

  it("returns the original list unchanged when it's empty", () => {
    // No plan-card tasks → nothing to merge into; same-reference
    // return spares React from a needless re-render via setState.
    const empty: PlanTask[] = [];
    expect(mergePlanTasks(empty, [{ content: "x", status: "completed" }])).toBe(
      empty,
    );
  });

  it("returns the original list unchanged when no todos match anything", () => {
    // Fast-path: an early ``todos_updated`` from an unrelated
    // run (separate chat session sharing the WS) must not blank
    // a PlanCard's checklist.
    const out = mergePlanTasks(planTasks, [
      { content: "Something else entirely", status: "completed" },
    ]);
    // Each task is exactly what it was — same statuses, same
    // activeForms.
    expect(out.map((t) => t.status)).toEqual(["completed", "pending", "pending"]);
  });

  it("updates statuses for matched tasks", () => {
    const out = mergePlanTasks(planTasks, [
      { content: "Run tests", status: "in_progress" },
      { content: "Ship", status: "in_progress" },
    ]);
    expect(out[0].status).toBe("completed"); // unchanged
    expect(out[1].status).toBe("in_progress");
    expect(out[2].status).toBe("in_progress");
  });

  it("preserves activeForm when the todo's is empty", () => {
    // The TodoStore sometimes doesn't carry activeForm (older
    // tools, sloppy callers). Falling back to the original
    // gerund keeps the rendered text stable as the task ticks.
    const out = mergePlanTasks(planTasks, [
      { content: "Run tests", status: "in_progress", activeForm: "" },
    ]);
    expect(out[1].activeForm).toBe("Running tests");
  });

  it("overrides activeForm when the todo's is non-empty", () => {
    const out = mergePlanTasks(planTasks, [
      {
        content: "Run tests",
        status: "in_progress",
        activeForm: "Executing the test suite",
      },
    ]);
    expect(out[1].activeForm).toBe("Executing the test suite");
  });

  it("keeps last-known status for tasks dropped from todos mid-flight", () => {
    // The agent may shorten the todo list as it works. The plan
    // card stays anchored to the plan the user approved — better
    // to show a stale-but-real status than to blank the row.
    const seeded: PlanTask[] = [
      { content: "Read code", status: "completed", activeForm: "Reading code" },
      { content: "Run tests", status: "in_progress", activeForm: "Running tests" },
    ];
    const out = mergePlanTasks(seeded, [
      // ``Read code`` dropped entirely from the new todo set.
      { content: "Run tests", status: "completed" },
    ]);
    expect(out[0].status).toBe("completed"); // last-known kept
    expect(out[1].status).toBe("completed"); // matched + updated
  });

  it("does not add NEW tasks from todos that aren't in the plan", () => {
    // The PlanCard reflects the user-approved plan, not the live
    // todo set. New todos appearing mid-run shouldn't graft
    // themselves into the card; the bare ``/todos`` view shows
    // those instead.
    const out = mergePlanTasks(planTasks, [
      { content: "Brand new task", status: "in_progress" },
    ]);
    expect(out).toHaveLength(planTasks.length);
    expect(out.every((t) => t.content !== "Brand new task")).toBe(true);
  });

  it("returns the original list when the todos value is unparseable", () => {
    // Non-array todos coming from a malformed push: noop, don't
    // clobber existing state.
    expect(mergePlanTasks(planTasks, undefined).map((t) => t.status)).toEqual([
      "completed",
      "pending",
      "pending",
    ]);
    expect(mergePlanTasks(planTasks, "garbage").map((t) => t.status)).toEqual([
      "completed",
      "pending",
      "pending",
    ]);
  });
});

// ── extractAttachedPaths ────────────────────────────────────
//
// Inverse of the BE's ``process_file_mentions``. The BE wraps
// referenced files in ``<attached-files>[Referenced files: a,
// b — read before responding]</attached-files>`` so the agent
// reads the list; on history restore we lift the paths back
// out so the FE can render attachment cards above the bubble.
// Falls back to @-mention regex when no hint line is found.

describe("extractAttachedPaths", () => {
  it("returns [] for empty content", () => {
    expect(extractAttachedPaths("")).toEqual([]);
  });

  it("returns [] when there are no @ mentions and no hint line", () => {
    expect(extractAttachedPaths("just a regular sentence")).toEqual([]);
  });

  it("lifts paths from the hint line (primary path)", () => {
    // The BE writes this exact shape — pin the format so a
    // drift on either side breaks loudly here.
    const content =
      "<attached-files>\n[Referenced files: src/a.py, src/b.ts — read before responding]\n</attached-files>\nthe prompt";
    expect(extractAttachedPaths(content)).toEqual(["src/a.py", "src/b.ts"]);
  });

  it("trims whitespace around each comma-separated path", () => {
    // The BE joins with ``", "`` but stripping is safer in
    // case a future BE writes with different spacing.
    const content =
      "[Referenced files:   a.py ,  b.py  ,c.py  — read before responding]";
    expect(extractAttachedPaths(content)).toEqual(["a.py", "b.py", "c.py"]);
  });

  it("filters empty splits (consecutive commas)", () => {
    // Defensive — ``a.py,,b.py`` shouldn't surface an empty
    // path that renders as a blank attachment chip.
    const content = "[Referenced files: a.py,,b.py — read before responding]";
    expect(extractAttachedPaths(content)).toEqual(["a.py", "b.py"]);
  });

  it("hint-line primary path wins over @-mention fallback", () => {
    // If both are present, hint line is canonical. The
    // @-fallback is for legacy / messages without the wrapper.
    const content =
      "[Referenced files: HINT.py — read before responding]\nsee @FALLBACK.py";
    expect(extractAttachedPaths(content)).toEqual(["HINT.py"]);
  });

  it("falls back to @-mention regex when no hint line", () => {
    // Messages from before the wrapper existed (or stripped
    // by a different code path) still surface mentions.
    expect(extractAttachedPaths("see @src/foo.py here")).toEqual(["src/foo.py"]);
  });

  it("collects multiple @-mentions in fallback path", () => {
    expect(extractAttachedPaths("@a.py and @b.py and @c.py")).toEqual([
      "a.py",
      "b.py",
      "c.py",
    ]);
  });

  it("fallback ignores email-style @ tokens", () => {
    // The @-regex requires whitespace or start-of-string
    // before ``@`` — same constraint as the BE side.
    expect(extractAttachedPaths("contact me at user@example.com")).toEqual([]);
  });
});

// ── correctStatsCtx ─────────────────────────────────────────
//
// Post-stream correction of the stats line's ``in`` token
// count. The live count from the streaming-event totals can
// undercount (model providers exclude the cached portion of
// the context); the BE fires a ``count_context_tokens`` RPC
// after the run completes and we patch the stats item with
// the authoritative number, marking ``corrected: true`` so
// the badge renders a check mark.

describe("correctStatsCtx", () => {
  const stats = {
    kind: "stats" as const,
    id: 1,
    runId: "run-a",
    inputTokens: 100,
    outputTokens: 50,
    reasoningTokens: 0,
    visibleThinkTokens: 0,
    visibleOutTokens: 25,
    duration: 1.5,
    corrected: false,
  };

  it("updates matching runId's stats with the new count and corrected flag", () => {
    const out = correctStatsCtx([stats], "run-a", 250);
    expect(out[0]).toMatchObject({
      kind: "stats",
      inputTokens: 250,
      corrected: true,
    });
  });

  it("leaves non-stats items untouched", () => {
    // Only ``stats`` items get patched; user/assistant/tool
    // items pass through identity-equal.
    const user = { kind: "user" as const, id: 2, text: "hi" };
    const out = correctStatsCtx([user, stats], "run-a", 250);
    expect(out[0]).toBe(user); // same reference
    expect(out[1]).not.toBe(stats); // new object for stats
  });

  it("leaves stats items with mismatched runId untouched", () => {
    const out = correctStatsCtx([stats], "different-run", 999);
    expect(out[0]).toBe(stats); // same reference, no mutation
  });

  it("patches all matching items (e.g. multiple stats for same run)", () => {
    // Edge case — if there are two stats items for the same
    // run somehow (BE bug, replay race), both get the same
    // correction.
    const stats2 = { ...stats, id: 3 };
    const out = correctStatsCtx([stats, stats2], "run-a", 250);
    expect((out[0] as typeof stats).inputTokens).toBe(250);
    expect((out[1] as typeof stats).inputTokens).toBe(250);
  });

  it("does not mutate the input items in place", () => {
    // The reducer must be pure — mutating in place would
    // break React's identity-equality-based memoisation.
    correctStatsCtx([stats], "run-a", 250);
    expect(stats.inputTokens).toBe(100); // unchanged
    expect(stats.corrected).toBe(false); // unchanged
  });
});

// ── restoredStatsItem ───────────────────────────────────────
//
// Builds a stats ChatItem from a persisted-history ``stats``
// turn the BE emits in ``get_chat_history``. The BE keys are
// integers/floats normally but defensive Number coercion
// handles None / missing / string-encoded shapes from older
// Agno serialisations.

describe("restoredStatsItem", () => {
  it("reads the standard numeric fields", () => {
    const out = restoredStatsItem(
      {
        run_id: "abc",
        input_tokens: 1000,
        output_tokens: 200,
        reasoning_tokens: 50,
        duration: 3.4,
      },
      "rendered assistant text",
    );
    expect(out.kind).toBe("stats");
    if (out.kind !== "stats") return;
    expect(out.runId).toBe("abc");
    expect(out.inputTokens).toBe(1000);
    expect(out.outputTokens).toBe(200);
    expect(out.reasoningTokens).toBe(50);
    expect(out.duration).toBe(3.4);
  });

  it("defaults missing fields to 0 (not NaN)", () => {
    // The Number(turn[k] ?? 0) || 0 pattern guards against
    // NaN from string fields the agent might have persisted.
    // NaN in inputTokens would render as "NaN tokens" in the
    // badge.
    const out = restoredStatsItem({}, "");
    if (out.kind !== "stats") return;
    expect(out.inputTokens).toBe(0);
    expect(out.outputTokens).toBe(0);
    expect(out.reasoningTokens).toBe(0);
    expect(out.duration).toBe(0);
  });

  it("coerces string numeric values", () => {
    // Older Agno serialisations carried integers as strings;
    // the Number() coercion handles them.
    const out = restoredStatsItem(
      { input_tokens: "1000", output_tokens: "200" },
      "",
    );
    if (out.kind !== "stats") return;
    expect(out.inputTokens).toBe(1000);
    expect(out.outputTokens).toBe(200);
  });

  it("coerces null to 0", () => {
    // ``null`` falls to ``Number(null) === 0``; the ``|| 0``
    // tail handles the NaN-from-arbitrary-string case.
    const out = restoredStatsItem({ input_tokens: null }, "");
    if (out.kind !== "stats") return;
    expect(out.inputTokens).toBe(0);
  });

  it("coerces non-numeric strings to 0 (NOT NaN)", () => {
    // The ``|| 0`` fallback catches NaN. Without it, the
    // stats badge would render "NaN" — visible bug.
    const out = restoredStatsItem({ input_tokens: "garbage" }, "");
    if (out.kind !== "stats") return;
    expect(out.inputTokens).toBe(0);
  });

  it("coerces non-string runId to string", () => {
    // ``runId`` is typed as string but the persisted shape
    // might surface it as a number. ``String()`` keeps the
    // type contract.
    const out = restoredStatsItem({ run_id: 12345 }, "");
    if (out.kind !== "stats") return;
    expect(out.runId).toBe("12345");
    expect(typeof out.runId).toBe("string");
  });

  it("defaults missing runId to empty string", () => {
    const out = restoredStatsItem({}, "");
    if (out.kind !== "stats") return;
    expect(out.runId).toBe("");
  });

  it("visibleThinkTokens is always 0 on restore (thinking stripped)", () => {
    // Persisted history strips ``<think>…</think>`` blocks
    // from assistant turns, so by construction the restored
    // ``out`` text has no thinking content.
    const out = restoredStatsItem(
      { input_tokens: 100, output_tokens: 50 },
      "any rendered text",
    );
    if (out.kind !== "stats") return;
    expect(out.visibleThinkTokens).toBe(0);
  });

  it("is marked corrected:true (no live RPC will patch it)", () => {
    // Historical stats land already-corrected — the
    // count_context_tokens RPC only fires for live runs. The
    // badge therefore renders without the "estimating…"
    // affordance.
    const out = restoredStatsItem({ input_tokens: 100 }, "");
    if (out.kind !== "stats") return;
    expect(out.corrected).toBe(true);
  });
});
