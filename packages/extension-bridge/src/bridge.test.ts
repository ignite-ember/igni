// @vitest-environment jsdom
/**
 * The guest half, driven the way a real extension page would drive it.
 *
 * The host here is a stub window: `connect` waits for a `hello` from
 * `window.parent`, so the test replaces that with something it can
 * post from and read what was posted to.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connect } from "./bridge";
import { BRIDGE_VERSION } from "./protocol";

/** Stands in for the host app's window. */
function fakeHost() {
  const posted: unknown[] = [];
  const host = {
    postMessage: (message: unknown) => {
      posted.push(message);
    },
  } as unknown as Window;
  Object.defineProperty(window, "parent", { value: host, configurable: true });
  return {
    posted,
    /** Deliver a message to the guest as the host would. */
    send(body: Record<string, unknown>, source: unknown = host) {
      const event = new MessageEvent("message", {
        data: { igni: BRIDGE_VERSION, ...body },
      });
      Object.defineProperty(event, "source", { value: source });
      window.dispatchEvent(event);
    },
    /** The last `call` the guest sent. */
    lastCall() {
      return [...posted]
        .reverse()
        .find((m) => (m as { kind: string }).kind === "call") as {
        id: string;
        method: string;
        args: Record<string, unknown>;
        nonce: string;
      };
    },
  };
}

const NONCE = "test-nonce";

function hello(over: Record<string, unknown> = {}) {
  return {
    kind: "hello",
    nonce: NONCE,
    extensionId: "igni.codeindex",
    params: {},
    theme: "dark",
    tokens: { "--bg": "#262624", "--fg": "#f0efea" },
    ...over,
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(window, "parent", {
    value: window,
    configurable: true,
  });
});

describe("connecting", () => {
  it("waits for the host to say hello, then reports what it was told", async () => {
    const host = fakeHost();
    const pending = connect();
    host.send(hello({ params: { tab: "browse" }, theme: "light" }));

    const bridge = await pending;

    expect(bridge.extensionId).toBe("igni.codeindex");
    expect(bridge.params).toEqual({ tab: "browse" });
    expect(bridge.theme).toBe("light");
  });

  it("ignores a hello from a window that is not the host", async () => {
    const host = fakeHost();
    const pending = connect({ timeoutMs: 50 });
    host.send(hello(), { not: "the host" });

    await expect(pending).rejects.toThrow(/no hello/);
  });

  it("refuses when the page is not framed at all", async () => {
    Object.defineProperty(window, "parent", {
      value: window,
      configurable: true,
    });
    await expect(connect()).rejects.toThrow(/not framed/);
  });

  it("gives up with a sentence rather than hanging", async () => {
    fakeHost();
    await expect(connect({ timeoutMs: 30 })).rejects.toThrow(
      /no hello from the host/,
    );
  });
});

describe("calling", () => {
  async function connected() {
    const host = fakeHost();
    const pending = connect();
    host.send(hello());
    return { host, bridge: await pending };
  }

  it("carries the nonce the host issued", async () => {
    const { host, bridge } = await connected();
    void bridge.rpc("codeindex_status");

    expect(host.lastCall().nonce).toBe(NONCE);
    expect(host.lastCall().method).toBe("codeindex_status");
    expect(host.lastCall().args).toEqual({});
  });

  it("resolves with the host's value", async () => {
    const { host, bridge } = await connected();
    const call = bridge.rpc<{ indexed: number }>("codeindex_status");
    const { id } = host.lastCall();

    host.send({ kind: "result", id, ok: true, value: { indexed: 42 } });

    await expect(call).resolves.toEqual({ indexed: 42 });
  });

  it("rejects with the host's refusal, as a sentence", async () => {
    const { host, bridge } = await connected();
    const call = bridge.rpc("run_shell");
    const { id } = host.lastCall();

    host.send({
      kind: "result",
      id,
      ok: false,
      error: "not permitted to call 'run_shell'",
    });

    await expect(call).rejects.toThrow(/not permitted/);
  });

  it("keeps two calls apart", async () => {
    const { host, bridge } = await connected();
    const first = bridge.rpc("codeindex_status");
    const firstId = host.lastCall().id;
    const second = bridge.rpc("codeindex_head");
    const secondId = host.lastCall().id;

    expect(firstId).not.toBe(secondId);
    host.send({ kind: "result", id: secondId, ok: true, value: "second" });
    host.send({ kind: "result", id: firstId, ok: true, value: "first" });

    await expect(first).resolves.toBe("first");
    await expect(second).resolves.toBe("second");
  });

  it("gives up on a call the host never answers", async () => {
    const { bridge } = await connected();
    const call = bridge.rpc("codeindex_status", {}, 40);
    await expect(call).rejects.toThrow(/timed out after 40ms/);
  });

  it("ignores a reply to a call it is not waiting on", async () => {
    const { host, bridge } = await connected();
    // A late answer to something that already timed out. Nothing to
    // resolve and no one to tell, so it must not throw either.
    expect(() =>
      host.send({ kind: "result", id: "999", ok: true, value: 1 }),
    ).not.toThrow();
    void bridge;
  });
});

describe("the rest of the surface", () => {
  async function connected() {
    const host = fakeHost();
    const pending = connect();
    host.send(hello());
    return { host, bridge: await pending };
  }

  it("asks the host to navigate", async () => {
    const { host, bridge } = await connected();
    bridge.navigate("artifacts");

    expect(host.posted).toContainEqual(
      expect.objectContaining({
        kind: "navigate",
        to: "artifacts",
        nonce: NONCE,
      }),
    );
  });

  it("tells the host when it is finished", async () => {
    const { host, bridge } = await connected();
    bridge.close();

    expect(host.posted).toContainEqual(
      expect.objectContaining({ kind: "close", nonce: NONCE }),
    );
  });

  it("follows the host's theme, and stops when asked", async () => {
    const { host, bridge } = await connected();
    const seen: string[] = [];
    const stop = bridge.onTheme((next) => seen.push(next));

    host.send({ kind: "theme", theme: "light", tokens: { "--bg": "#ffffff" } });
    expect(bridge.theme).toBe("light");
    expect(bridge.tokens["--bg"]).toBe("#ffffff");

    stop();
    host.send({ kind: "theme", theme: "dark", tokens: { "--bg": "#262624" } });

    expect(seen).toEqual(["light"]);
    // The reading still follows even with nobody watching.
    expect(bridge.theme).toBe("dark");
  });
});
