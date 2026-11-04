// @vitest-environment jsdom
/**
 * `bridge.host` and `bridge.on`: the host object model, from a page.
 *
 * The page asks; the window it is framed in forwards the request to the
 * backend (`host_model`) and posts the reply back. Here the window is a
 * stub that answers like that forwarding would.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { connect } from "./bridge";
import { HostError } from "./hostModel";
import { BRIDGE_VERSION, type HostRequestMessage } from "./protocol";

const NONCE = "test-nonce";

function framedPage() {
  const posted: HostRequestMessage[] = [];
  const host = {
    postMessage: (message: unknown) => {
      if ((message as { kind: string }).kind === "host") {
        posted.push(message as HostRequestMessage);
      }
    },
  } as unknown as Window;
  Object.defineProperty(window, "parent", { value: host, configurable: true });
  function send(body: Record<string, unknown>) {
    const event = new MessageEvent("message", {
      data: { igni: BRIDGE_VERSION, ...body },
    });
    Object.defineProperty(event, "source", { value: host });
    window.dispatchEvent(event);
  }
  return {
    posted,
    send,
    reply(id: string, body: Record<string, unknown>) {
      send({ kind: "host-reply", id, ...body });
    },
    async connected() {
      const pending = connect();
      send({
        kind: "hello",
        nonce: NONCE,
        extensionId: "acme.reviewer",
        params: {},
        theme: "dark",
        tokens: {},
      });
      return pending;
    },
  };
}

afterEach(() => {
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("bridge.host", () => {
  it("reads a property through the window, with the nonce", async () => {
    const page = framedPage();
    const bridge = await page.connected();

    const read = bridge.host.chat.processing.get();
    const [request] = page.posted;
    expect(request).toMatchObject({
      kind: "host",
      nonce: NONCE,
      op: "get",
      path: "chat.processing",
      window: null,
    });
    page.reply(request.id, { ok: true, result: true });

    await expect(read).resolves.toBe(true);
  });

  it("calls a method with its parameters by name", async () => {
    const page = framedPage();
    const bridge = await page.connected();

    const posting = bridge.host.chat.post("Two issues in auth.py.");
    const [request] = page.posted;
    expect(request).toMatchObject({
      op: "call",
      path: "chat.post",
      params: { text: "Two issues in auth.py." },
    });
    page.reply(request.id, { ok: true, result: null });
    await expect(posting).resolves.toBeNull();
  });

  it("asks igni's decision model at the root, and an unloadable model is unavailable", async () => {
    const page = framedPage();
    const bridge = await page.connected();
    const questions = [{ id: "urgent", kind: "noul", question: "Is `text` urgent?" }];

    const deciding = bridge.host.decide({ text: "It is down" }, questions);
    const [request] = page.posted;
    expect(request).toMatchObject({
      op: "call",
      path: "decide",
      params: { context: { text: "It is down" }, questions },
    });
    const decision = {
      model: "english",
      answers: {
        urgent: {
          kind: "noul",
          answer: "yes",
          confidence: 0.8,
          probabilities: { yes: 0.8, no: 0.2 },
        },
      },
    };
    page.reply(request.id, { ok: true, result: decision });
    await expect(deciding).resolves.toEqual(decision);

    const again = bridge.host.decide({ text: "x" }, questions);
    page.reply(page.posted[1].id, {
      ok: false,
      error: { code: "unavailable", message: "igni's decision model could not be loaded" },
    });
    await expect(again).rejects.toMatchObject({ code: "unavailable" });
  });

  it("writes a writable property and addresses another window by id", async () => {
    const page = framedPage();
    const bridge = await page.connected();

    const writing = bridge.host.windows.ui("w-2").route.set([]);
    const [request] = page.posted;
    expect(request).toMatchObject({
      op: "set",
      path: "ui.route",
      window: "w-2",
      value: [],
    });
    page.reply(request.id, { ok: true });
    await writing;
  });

  it("addresses any session's chat by id, and host.chat by none", async () => {
    const page = framedPage();
    const bridge = await page.connected();

    const posting = bridge.host.sessions.chat("s-2").post("The index is ready.");
    const [request] = page.posted;
    expect(request).toMatchObject({
      op: "call",
      path: "chat.post",
      session: "s-2",
      params: { text: "The index is ready." },
    });
    page.reply(request.id, { ok: true, result: null });
    await posting;

    const here = bridge.host.chat.post("here");
    const second = page.posted[1];
    expect(second.session).toBeUndefined();
    page.reply(second.id, { ok: true, result: null });
    await here;
  });

  it("waits for a dialog's answer as long as the person takes", async () => {
    const page = framedPage();
    const bridge = await page.connected();
    vi.useFakeTimers();
    try {
      const asking = bridge.host.ui.show_dialog({ title: "Ship it?" });
      const reading = bridge.host.chat.processing.get();
      const failed = reading.catch((e: unknown) => e);
      // Long past the bridge's limit for anything else…
      vi.advanceTimersByTime(10 * 60_000);
      expect(await failed).toBeInstanceOf(HostError);
      // …the dialog is still waiting, and gets its answer.
      const [dialog] = page.posted;
      expect(dialog).toMatchObject({ op: "call", path: "ui.show_dialog" });
      page.reply(dialog.id, { ok: true, result: { button: "ok", value: null } });
      await expect(asking).resolves.toEqual({ button: "ok", value: null });
    } finally {
      vi.useRealTimers();
    }
  });

  it("rejects a refusal with the host's code", async () => {
    const page = framedPage();
    const bridge = await page.connected();

    const reading = bridge.host.mcp.servers.get();
    page.reply(page.posted[0].id, {
      ok: false,
      error: { code: "not_granted", message: "'acme.reviewer' is not granted 'mcp'" },
    });

    const error = await reading.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(HostError);
    expect((error as HostError).code).toBe("not_granted");
  });

  it("refuses to write a property that is not writable without asking", async () => {
    const page = framedPage();
    const bridge = await page.connected();
    await expect(bridge.host.chat.processing.set(true)).rejects.toThrow(
      /cannot be set/,
    );
    expect(page.posted).toEqual([]);
  });
});

describe("bridge.on", () => {
  it("subscribes on the first handler and delivers the value", async () => {
    const page = framedPage();
    const bridge = await page.connected();
    const seen: string[] = [];

    bridge.on(bridge.host.sessions.current_changed, (change) => {
      seen.push(change.session_id);
    });
    const [subscribe] = page.posted;
    expect(subscribe).toMatchObject({
      op: "subscribe",
      signals: ["session.switched"],
    });

    page.send({
      kind: "signal",
      signal: "session.switched",
      value: { session_id: "s-9", window: null },
    });
    page.send({ kind: "signal", signal: "chat.run_completed", value: {} });

    expect(seen).toEqual(["s-9"]);
  });

  it("unsubscribes when the last handler goes", async () => {
    const page = framedPage();
    const bridge = await page.connected();
    const off = bridge.on(bridge.host.chat.run_completed, () => {});
    const offAgain = bridge.on(bridge.host.chat.run_completed, () => {});
    expect(page.posted.map((p) => p.op)).toEqual(["subscribe"]);

    off();
    expect(page.posted.map((p) => p.op)).toEqual(["subscribe"]);
    offAgain();
    expect(page.posted.map((p) => p.op)).toEqual(["subscribe", "unsubscribe"]);
  });
});
