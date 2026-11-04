// @vitest-environment jsdom
/**
 * The whole app, mounted against the fixture backend the Playwright suite
 * uses (`e2e/fixtures/backend.ts`), over a real loopback WebSocket.
 *
 * `App.tsx` is ~4000 lines with its state in one component, so there is
 * no seam smaller than "the app and a backend" to test it through. The
 * E2E suite drives it in a browser; this drives the same flows in jsdom,
 * which is quick enough to run on every `vitest run` and is what makes
 * its coverage visible to the unit-coverage floor. What is not here —
 * layout, scrolling, visual baselines, multiple windows — stays in E2E.
 *
 * The only stand-ins are what jsdom lacks: a WebSocket (carried over
 * Node's `ws`, so the backend is the fixture's real server),
 * ResizeObserver, matchMedia, and Virtuoso's viewport size.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { WebSocket as NodeWebSocket } from "ws";

import App from "./App";
import {
  signedInStatus,
  startFixtureBackend,
  type Envelope,
  type FixtureBackend,
  type RpcHandler,
} from "../e2e/fixtures/backend";

/** The browser-facing WebSocket, carried over Node's `ws`. */
class BridgedWebSocket {
  static OPEN = 1;
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  private inner: NodeWebSocket;
  constructor(url: string, protocols?: string[]) {
    this.inner = new NodeWebSocket(url, protocols);
    this.inner.on("open", () => {
      this.readyState = 1;
      act(() => this.onopen?.({}));
    });
    this.inner.on("message", (data) => {
      act(() => this.onmessage?.({ data: data.toString() }));
    });
    this.inner.on("close", (code) => {
      this.readyState = 3;
      act(() => this.onclose?.({ code }));
    });
    this.inner.on("error", () => this.onerror?.({}));
  }
  send(data: string) {
    this.inner.send(data);
  }
  close() {
    this.inner.close();
  }
}

let be: FixtureBackend;

// A real socket and the whole app: under a full parallel run, a second is
// not always enough for a reply to land.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });

beforeAll(() => {
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = BridgedWebSocket;
  class FakeResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = FakeResizeObserver;
  window.matchMedia ??= ((q: string) => ({
    matches: false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    onchange: null,
    dispatchEvent: () => false,
  })) as never;
  // Quiet the app's own diagnostics (WS URL, unhandled fixture RPCs).
  console.info = () => {};
});

async function boot(handlers: Record<string, RpcHandler> = {}) {
  be = await startFixtureBackend({
    rpcHandlers: {
      list_extensions: () => ({ extensions: [] }),
      get_chat_history: () => [],
      get_pending_messages: () => [],
      get_workspace_trust: () => ({
        state: "trusted",
        can_trust: true,
        reason: "",
        path: "/tmp/test-project",
        remote: null,
        policy_mode: "ask",
        gated: "",
      }),
      ...handlers,
    },
  });
  window.history.replaceState({}, "", `/?ws=${encodeURIComponent(be.url)}`);
  render(
    <VirtuosoMockContext.Provider value={{ viewportHeight: 4000, itemHeight: 40 }}>
      <App />
    </VirtuosoMockContext.Provider>,
  );
  const editor = await screen.findByRole("textbox", {}, { timeout: 5000 });
  await waitFor(() => expect(editor.getAttribute("data-placeholder")).toMatch(/Message/), {
    timeout: 5000,
  });
  return editor;
}

const received = (type: string) => be.received().filter((m) => m.type === type);
const rpcs = () => be.received().filter((m) => m.type === "rpc_request").map((m) => m.method);

function typeAndSend(editor: HTMLElement, text: string) {
  act(() => {
    editor.textContent = text;
    const range = document.createRange();
    range.setStart(editor.firstChild!, text.length);
    range.collapse(true);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    fireEvent.input(editor);
  });
  act(() => {
    fireEvent.keyDown(editor, { key: "Enter" });
  });
}

async function sentRun(text: string): Promise<string> {
  await waitFor(() =>
    expect(received("user_message").find((m) => String(m.text).includes(text))).toBeTruthy(),
  );
  return String(received("user_message").find((m) => String(m.text).includes(text))!.id);
}

function push(envelope: Envelope) {
  be.pushEvent(envelope);
}

beforeEach(() => {
  window.localStorage?.clear?.();
});

afterEach(async () => {
  cleanup();
  await be?.close();
});

describe("starting up", () => {
  it("binds its session and asks for what the window shows", async () => {
    await boot();
    await waitFor(() =>
      expect(rpcs()).toEqual(
        expect.arrayContaining([
          "get_session_id",
          "get_chat_history",
          "get_status",
          "list_sessions",
          "get_workspace_trust",
          "list_extensions",
        ]),
      ),
    );
    expect(screen.getAllByText("test-model").length).toBeGreaterThan(0);
  });

  it("restores the conversation from the backend's history", async () => {
    await boot({
      get_chat_history: () => [
        { role: "user", content: "what's 2+2?", run_id: "r-1" },
        { role: "assistant", content: "It's 4.", run_id: "r-1" },
      ],
    });
    expect(await screen.findByText("what's 2+2?")).toBeTruthy();
    expect(await screen.findByText("It's 4.")).toBeTruthy();
  });

  it("puts the sign-in wall in front without a cloud account", async () => {
    be = await startFixtureBackend({
      rpcHandlers: {
        list_extensions: () => ({ extensions: [] }),
        get_status: () => signedInStatus({ cloud_email: "", cloud_connected: false }),
      },
    });
    window.history.replaceState({}, "", `/?ws=${encodeURIComponent(be.url)}`);
    render(<App />);
    expect(await screen.findByText("Sign in to igni", {}, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByText(/Enter to send/)).toBeNull();
  });

  it("asks whether to trust a folder that has not been decided", async () => {
    await boot({
      get_workspace_trust: () => ({
        state: "ask",
        can_trust: true,
        reason: "You have not decided yet.",
        path: "/tmp/test-project",
        remote: null,
        policy_mode: "ask",
        gated: "project tools",
      }),
    });
    expect(await screen.findByText("Trust this folder?", {}, { timeout: 5000 })).toBeTruthy();
  });
});

describe("a conversation", () => {
  it("sends what was typed and renders the streamed reply", async () => {
    const editor = await boot();
    typeAndSend(editor, "hello, are you working?");
    const runId = await sentRun("hello, are you working?");
    expect(await screen.findByText("hello, are you working?")).toBeTruthy();

    push({ type: "run_started", id: runId, run_id: runId, parent_run_id: null, agent_name: "igni", model: "test-model" });
    push({ type: "content_delta", id: runId, text: "Yes, ", is_thinking: false });
    push({ type: "content_delta", id: runId, text: "I'm here.", is_thinking: false });
    push({
      type: "run_completed",
      id: runId,
      run_id: runId,
      parent_run_id: null,
      input_tokens: 12,
      output_tokens: 5,
      reasoning_tokens: 0,
      duration: 1.2,
    });
    push({ type: "stream_end", id: runId });
    expect(await screen.findByText("Yes, I'm here.")).toBeTruthy();
  });

  it("shows a tool call and its result", async () => {
    const editor = await boot();
    typeAndSend(editor, "list files");
    const runId = await sentRun("list files");
    push({ type: "run_started", id: runId, run_id: runId, parent_run_id: null, agent_name: "igni", model: "test-model" });
    push({
      type: "tool_started",
      id: runId,
      run_id: runId,
      tool_name: "run_shell_command",
      friendly_name: "Bash",
      args_summary: "ls -la",
    });
    push({
      type: "tool_completed",
      id: runId,
      run_id: runId,
      summary: "3 files",
      full_result: "a\nb\nc",
      has_markup: false,
      diff_rows: null,
      is_error: false,
    });
    push({ type: "content_delta", id: runId, text: "Three files.", is_thinking: false });
    push({ type: "stream_end", id: runId });
    expect(await screen.findByText("Three files.")).toBeTruthy();
    expect(screen.getAllByText(/ls -la/).length).toBeGreaterThan(0);
  });

  it("says what went wrong when a run fails", async () => {
    const editor = await boot();
    typeAndSend(editor, "do the thing");
    const runId = await sentRun("do the thing");
    push({ type: "run_error", id: runId, error: "The model provider refused the request." });
    push({ type: "stream_end", id: runId });
    expect(
      (await screen.findAllByText(/The model provider refused the request\./)).length,
    ).toBeGreaterThan(0);
  });

  it("asks before a tool runs, and sends the answer", async () => {
    const editor = await boot();
    typeAndSend(editor, "delete the build dir");
    const runId = await sentRun("delete the build dir");
    push({
      type: "run_paused",
      id: runId,
      run_id: runId,
      requirements: [
        {
          type: "hitl_request",
          requirement_id: "req-1",
          tool_name: "run_shell_command",
          friendly_name: "Bash",
          tool_args: { command: "rm -rf build" },
          details: "Removes the build directory.",
          agent_path: ["igni"],
        },
      ],
    });
    push({ type: "stream_end", id: runId });
    expect((await screen.findAllByText(/rm -rf build/, {}, { timeout: 5000 })).length).toBeGreaterThan(0);
    const allow = screen
      .getAllByRole("button")
      .find((b) => /^(Allow|Yes|Approve|Run)/i.test(b.textContent || ""));
    expect(allow).toBeTruthy();
    fireEvent.click(allow!);
    await waitFor(() => expect(received("hitl_response_batch")).toHaveLength(1));
    expect(received("hitl_response_batch")[0].decisions).toEqual([
      expect.objectContaining({ requirement_id: "req-1", action: "confirm" }),
    ]);
  });

  it("runs a slash command as a command, not a message", async () => {
    const editor = await boot();
    typeAndSend(editor, "/help");
    await waitFor(() => expect(received("command")).toHaveLength(1));
    expect(received("command")[0].text).toBe("/help");
    expect(received("user_message")).toHaveLength(0);
  });

  it("updates the footer when the backend pushes a new status", async () => {
    await boot();
    push(signedInStatus({ model: "igni-1.0-27b" }));
    expect((await screen.findAllByText("igni-1.0-27b")).length).toBeGreaterThan(0);
  });

  it("goes back to Connecting… when the backend goes away mid-run", async () => {
    const editor = await boot();
    typeAndSend(editor, "hi");
    await sentRun("hi");
    await be.close();
    await waitFor(() => expect(editor.getAttribute("data-placeholder")).toMatch(/Connecting/), {
      timeout: 5000,
    });
  });
});

describe("pages", () => {
  it.each([
    ["Plugins", "get_plugin_details"],
    ["MCPs", "get_mcp_server_details"],
  ])("opens %s from the sidebar", async (label, method) => {
    await boot({
      get_plugin_details: () => [],
      get_marketplaces: () => [],
      ensure_mcp: () => null,
      get_mcp_server_details: () => [],
    });
    fireEvent.click(screen.getByText(label));
    await waitFor(() => expect(rpcs()).toContain(method), { timeout: 5000 });
  });
});

/** Answer the next slash command the way the backend does: one
 *  `command_result` carrying the request's id. */
async function answerCommand(
  text: string,
  result: { action?: string; content?: string; display_content?: string; kind?: string },
) {
  await waitFor(() => expect(received("command").find((m) => m.text === text)).toBeTruthy());
  const id = received("command").find((m) => m.text === text)!.id;
  push({
    type: "command_result",
    id,
    kind: result.kind ?? "action",
    content: result.content ?? "",
    display_content: result.display_content ?? "",
    action: result.action ?? "",
  });
}

const PAGE_RPCS: Record<string, RpcHandler> = {
  ensure_mcp: () => null,
  get_mcp_server_details: () => [],
  get_scheduled_tasks: () => [],
  get_hooks_details: () => [],
  get_agent_details: () => [],
  get_skill_details: () => [],
  loop_status: () => ({ active: false }),
  list_background_processes: () => [],
  get_plugin_details: () => [],
  get_marketplaces: () => [],
};

describe("slash commands that open a page", () => {
  it.each([
    ["/mcp", "mcp", "get_mcp_server_details"],
    ["/schedule", "schedule", "get_scheduled_tasks"],
    ["/hooks", "hooks", "get_hooks_details"],
    ["/agents", "agents", "get_agent_details"],
    ["/skills", "skills", "get_skill_details"],
    ["/loop", "loop", "loop_status"],
    ["/watcher", "watcher", "list_background_processes"],
    ["/plugins", "plugins", "get_plugin_details"],
  ])("%s opens its page", async (command, action, method) => {
    const editor = await boot(PAGE_RPCS);
    typeAndSend(editor, command);
    await answerCommand(command, { action });
    await waitFor(() => expect(rpcs()).toContain(method));
  });
});

describe("slash commands that change the conversation", () => {
  it("/clear empties the view and follows the renewed session", async () => {
    let id = "test-session-001";
    const editor = await boot({
      get_session_id: () => id,
      get_chat_history: () => [{ role: "user", content: "old question", run_id: "r-0" }],
    });
    expect(await screen.findByText("old question")).toBeTruthy();
    id = "test-session-002";
    typeAndSend(editor, "/clear");
    await answerCommand("/clear", { action: "clear" });
    await waitFor(() => expect(screen.queryByText("old question")).toBeNull());
    await waitFor(() =>
      expect(be.received().filter((m) => m.method === "get_session_id").length).toBeGreaterThan(1),
    );
  });

  it("/fork continues in the new session with the history cloned", async () => {
    const editor = await boot({
      get_chat_history: (args) =>
        args.session_id === "forked-1"
          ? [{ role: "assistant", content: "cloned answer", run_id: "r-9" }]
          : [],
    });
    typeAndSend(editor, "/fork");
    await answerCommand("/fork", { action: "fork", content: "forked-1" });
    expect(await screen.findByText(/Forked to a new session/)).toBeTruthy();
  });

  it("/fork without a session id says it failed", async () => {
    const editor = await boot();
    typeAndSend(editor, "/fork");
    await answerCommand("/fork", { action: "fork", content: "" });
    expect(await screen.findByText(/Fork failed/)).toBeTruthy();
  });

  it("a skill that expands to a prompt runs it as a message", async () => {
    const editor = await boot();
    typeAndSend(editor, "/commit");
    await answerCommand("/commit", { action: "run_prompt", content: "Write a commit message." });
    await sentRun("Write a commit message.");
  });

  it("/compact shows the summary card", async () => {
    const editor = await boot();
    typeAndSend(editor, "/compact");
    await answerCommand("/compact", {
      action: "compact",
      content: "Context compacted: 40k → 6k tokens.",
      display_content: "We fixed the parser.",
    });
    expect((await screen.findAllByText(/Context compacted/)).length).toBeGreaterThan(0);
  });

  it("/quit says how to quit instead", async () => {
    const editor = await boot();
    typeAndSend(editor, "/quit");
    await answerCommand("/quit", { action: "quit" });
    expect(await screen.findByText(/close button to quit/)).toBeTruthy();
  });

  it("a plain markdown result is shown in the chat", async () => {
    const editor = await boot();
    typeAndSend(editor, "/status");
    await answerCommand("/status", { kind: "markdown", content: "**All good**" });
    expect(await screen.findByText("All good")).toBeTruthy();
  });
});

describe("shell mode", () => {
  it("runs a $ line on the backend and shows its output", async () => {
    const editor = await boot({ run_shell: () => ({ output: "hello-from-shell", exit_code: 0 }) });
    typeAndSend(editor, "$echo hi");
    await waitFor(() => expect(be.received().find((m) => m.method === "run_shell")).toBeTruthy());
    expect(be.received().find((m) => m.method === "run_shell")!.args).toEqual({ command: "echo hi" });
    expect((await screen.findAllByText(/hello-from-shell/)).length).toBeGreaterThan(0);
  });
});

describe("while a run is going", () => {
  it("queues a second message instead of starting a second run", async () => {
    const editor = await boot();
    typeAndSend(editor, "first");
    const runId = await sentRun("first");
    push({ type: "run_started", id: runId, run_id: runId, parent_run_id: null, agent_name: "igni", model: "test-model" });
    typeAndSend(editor, "second");
    await waitFor(() => expect(received("queue_message")).toHaveLength(1));
    expect(received("queue_message")[0].text).toBe("second");
    expect(received("user_message")).toHaveLength(1);
  });

  it("Escape cancels it", async () => {
    const editor = await boot();
    typeAndSend(editor, "long job");
    const runId = await sentRun("long job");
    push({ type: "run_started", id: runId, run_id: runId, parent_run_id: null, agent_name: "igni", model: "test-model" });
    await waitFor(() => expect(screen.queryAllByText("long job").length).toBeGreaterThan(0));
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    await waitFor(() => expect(received("cancel")).toHaveLength(1));
  });
});

