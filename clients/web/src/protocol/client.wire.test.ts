/**
 * What the client does with messages once a socket is open: correlation,
 * streams, session routing, and what a dropped connection does to calls
 * still waiting.
 *
 * ``client.reconnect.test.ts`` covers the connection lifecycle; this file
 * covers the traffic over it, with the same stubbed ``WebSocket``. The
 * failures here are quiet ones — a reply delivered to the wrong caller, a
 * chat rendering another tab's session, a spinner waiting on a socket that
 * died — so each is pinned as the user would notice it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  close(): void {}
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  receive(msg: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  fail(code = 1006): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  get last(): Record<string, unknown> {
    return this.sent[this.sent.length - 1];
  }
}

type Client = import("./client").IgniClient;
let mod: typeof import("./client");
let win: Record<string, unknown>;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;
  win = {
    location: { search: "" },
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  };
  (globalThis as { window?: unknown }).window = win;
  (globalThis as { document?: unknown }).document = { querySelector: () => null };
  vi.spyOn(console, "info").mockImplementation(() => undefined);
  mod = await import("./client");
});

afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  vi.restoreAllMocks();
});

function connected(): { client: Client; ws: FakeWebSocket } {
  const client = new mod.IgniClient("ws://test", "");
  client.connect();
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  ws.open();
  return { client, ws };
}

describe("an RPC", () => {
  it("resolves with the result of the reply carrying its id", async () => {
    const { client, ws } = connected();
    const pending = client.rpc("get_hooks_details", { a: 1 });
    expect(ws.last).toMatchObject({ type: "rpc_request", method: "get_hooks_details", args: { a: 1 } });
    ws.receive({ type: "rpc_response", id: ws.last.id, result: ["x"] });
    await expect(pending).resolves.toEqual(["x"]);
  });

  it("rejects with the backend's error", async () => {
    const { client, ws } = connected();
    const pending = client.rpc("nope");
    ws.receive({ type: "rpc_response", id: ws.last.id, error: "unknown method: nope" });
    await expect(pending).rejects.toThrow("unknown method: nope");
  });

  it("resolves a typed reply as itself", async () => {
    const { client, ws } = connected();
    const pending = client.rpc("get_status");
    ws.receive({ type: "status_update", id: ws.last.id, model: "m" });
    await expect(pending).resolves.toMatchObject({ type: "status_update", model: "m" });
  });

  it("times out with the method's name, and a late reply goes nowhere", async () => {
    const { client, ws } = connected();
    const listener = vi.fn();
    client.onEvent(listener);
    const pending = client.rpc("slow", {}, 1_000);
    const id = ws.last.id;
    vi.advanceTimersByTime(1_000);
    await expect(pending).rejects.toThrow("RPC slow timed out");
    ws.receive({ type: "rpc_response", id, result: 1 });
    // Not a waiter any more, not a stream: it reaches the listeners as
    // an uncorrelated message instead of resolving anything.
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("fails at once when there is no socket", async () => {
    const client = new mod.IgniClient("ws://test", "");
    await expect(client.rpc("get_status")).rejects.toThrow("not connected to backend");
  });

  it("made while disconnected leaves no timer behind", async () => {
    // A panel polling every few seconds while the backend is down would
    // otherwise pile up a minute-long timer per attempt.
    const client = new mod.IgniClient("ws://test", "");
    const before = vi.getTimerCount();
    await expect(client.rpc("get_scheduled_tasks")).rejects.toThrow("not connected");
    expect(vi.getTimerCount()).toBe(before);
  });

  it("is rejected when the connection drops under it", async () => {
    const { client, ws } = connected();
    const pending = client.rpc("get_status");
    ws.fail();
    await expect(pending).rejects.toThrow("connection closed");
  });

  it("carries the convenience calls' method names and arguments", () => {
    const { client, ws } = connected();
    const calls: [() => Promise<unknown>, string, Record<string, unknown>][] = [
      [() => client.completeFiles("src/"), "complete_files", { query: "src/", limit: 50 }],
      [
        () => client.searchChat("s1", "bug", 5),
        "search_chat",
        { session_id: "s1", query: "bug", limit: 5 },
      ],
      [() => client.runShell("ls"), "run_shell", { command: "ls" }],
      [() => client.login(), "login", {}],
      [() => client.approvePlan("r1"), "approve_plan", { run_id: "r1" }],
      [() => client.dismissPlan("r2"), "dismiss_plan", { run_id: "r2" }],
    ];
    for (const [call, method, args] of calls) {
      void call().catch(() => undefined);
      expect(ws.last).toMatchObject({ type: "rpc_request", method, args });
    }
  });
});

describe("request and reply", () => {
  it("a slash command resolves with the reply to its id", async () => {
    const { client, ws } = connected();
    const pending = client.handleCommand("/help");
    expect(ws.last).toMatchObject({ type: "command", text: "/help" });
    ws.receive({ type: "command_result", id: ws.last.id, content: "help text" });
    await expect(pending).resolves.toMatchObject({ content: "help text" });
  });

  it("a model switch and an MCP toggle are correlated the same way", async () => {
    const { client, ws } = connected();
    const model = client.switchModel("igni-1.0-27b");
    expect(ws.last).toMatchObject({ type: "model_switch", model_name: "igni-1.0-27b" });
    ws.receive({ type: "info", id: ws.last.id, text: "switched" });
    await expect(model).resolves.toMatchObject({ text: "switched" });

    const toggle = client.mcpToggle("github", false);
    expect(ws.last).toMatchObject({ type: "mcp_toggle", server_name: "github", connect: false });
    ws.receive({ type: "info", id: ws.last.id, text: "disconnected" });
    await expect(toggle).resolves.toMatchObject({ text: "disconnected" });
  });

  it("fails at once without a socket and times out without a reply", async () => {
    const offline = new mod.IgniClient("ws://test", "");
    await expect(offline.handleCommand("/help")).rejects.toThrow("not connected");

    const { client } = connected();
    const pending = client.handleCommand("/help");
    vi.advanceTimersByTime(60_000);
    await expect(pending).rejects.toThrow("request timed out");
  });
});

describe("a streamed run", () => {
  it("delivers every event of its run, then resolves on stream_end", async () => {
    const { client, ws } = connected();
    client.clientId = "c1";
    const seen: unknown[] = [];
    let done = false;
    const run = client
      .runMessage("hi", (m) => seen.push(m.type), { "a.ts": "x" }, { force: true })
      .then(() => {
        done = true;
      });
    expect(ws.last).toMatchObject({
      type: "user_message",
      text: "hi",
      client_id: "c1",
      file_contents: { "a.ts": "x" },
      force: true,
    });
    const id = ws.last.id;
    ws.receive({ type: "content_delta", id, text: "he" });
    ws.receive({ type: "content_delta", id, text: "llo" });
    await Promise.resolve();
    expect(done).toBe(false);
    ws.receive({ type: "stream_end", id });
    await run;
    expect(seen).toEqual(["content_delta", "content_delta"]);
  });

  it("ends, rather than hangs, when the connection drops", async () => {
    const { client, ws } = connected();
    const run = client.resolveHitlBatch(
      [{ requirement_id: "r", action: "confirm", choice: "once" }],
      () => undefined,
    );
    expect(ws.last).toMatchObject({ type: "hitl_response_batch" });
    ws.fail();
    await expect(run).resolves.toBeUndefined();
  });

  it("fails at once without a socket", async () => {
    const client = new mod.IgniClient("ws://test", "");
    await expect(client.runMessage("hi", () => undefined)).rejects.toThrow("not connected");
  });
});

describe("session routing", () => {
  it("stamps the bound session on what it sends, unless one is named", () => {
    const { client, ws } = connected();
    client.sessionId = "s-mine";
    client.cancel();
    expect(ws.last).toEqual({ type: "cancel", session_id: "s-mine" });
    void client.searchChat("other", "q").catch(() => undefined);
    expect(ws.last.session_id).toBe("s-mine");
  });

  it("drops another session's broadcasts and passes its own and unstamped ones", () => {
    const { client, ws } = connected();
    client.sessionId = "s-mine";
    const seen: unknown[] = [];
    client.onEvent((m) => seen.push((m as { text?: string }).text));
    ws.receive({ type: "info", session_id: "s-other", text: "theirs" });
    ws.receive({ type: "info", session_id: "s-mine", text: "mine" });
    ws.receive({ type: "info", text: "global" });
    expect(seen).toEqual(["mine", "global"]);
  });

  it("a reply to our own request is ours whatever session it names", async () => {
    const { client, ws } = connected();
    client.sessionId = "s-mine";
    const pending = client.rpc("get_status");
    ws.receive({ type: "rpc_response", id: ws.last.id, session_id: "s-other", result: 7 });
    await expect(pending).resolves.toBe(7);
  });

  it("learns its client id from the welcome and still tells listeners", () => {
    const { client, ws } = connected();
    const listener = vi.fn();
    client.onEvent(listener);
    ws.receive({ type: "welcome", client_id: "c-42" });
    expect(client.clientId).toBe("c-42");
    expect(listener).toHaveBeenCalledOnce();
  });
});

describe("fire-and-forget messages", () => {
  it("queues a message with the client id", () => {
    const { client, ws } = connected();
    client.clientId = "c1";
    client.queueMessage("and then this");
    expect(ws.last).toEqual({ type: "queue_message", text: "and then this", client_id: "c1" });
  });

  it("cancels a login", () => {
    const { client, ws } = connected();
    client.cancelLogin();
    expect(ws.last).toEqual({ type: "cancel_login" });
  });

  it("sends a typing draft at most every 100ms, always the latest", () => {
    const { client, ws } = connected();
    client.clientId = "c1";
    client.sendTyping("h");
    client.sendTyping("he");
    client.sendTyping("hel");
    expect(ws.sent).toHaveLength(0);
    vi.advanceTimersByTime(100);
    expect(ws.sent).toEqual([{ type: "typing", text: "hel", client_id: "c1" }]);
    client.sendTyping("hell");
    vi.advanceTimersByTime(100);
    expect(ws.last).toMatchObject({ text: "hell" });
  });

  it("drops a typing draft quietly when disconnected", () => {
    const client = new mod.IgniClient("ws://test", "");
    client.sendTyping("x");
    expect(() => vi.advanceTimersByTime(100)).not.toThrow();
  });
});

describe("the native folder picker", () => {
  it("is undefined where the shell offers none", async () => {
    await expect(mod.pickNativeDirectory("/a")).resolves.toBeUndefined();
  });

  it("passes the start folder and returns the pick", async () => {
    const pick = vi.fn(async (start?: string) => `${start}/b`);
    win.__EMBER_PICK_DIR__ = pick;
    await expect(mod.pickNativeDirectory("/a")).resolves.toBe("/a/b");
  });

  it("reads a picker that throws as a cancel", async () => {
    win.__EMBER_PICK_DIR__ = async () => {
      throw new Error("dialog crashed");
    };
    await expect(mod.pickNativeDirectory()).resolves.toBeNull();
  });
});
