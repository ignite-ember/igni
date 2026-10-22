// @vitest-environment jsdom
/**
 * The host refuses what it should, and only what it should.
 *
 * Every test here drives the component the way a frame would: by
 * dispatching a `message` event. That is the real entry point, and it
 * is the one an attacker has too — so the cases that matter are the
 * ones where the message is well-formed and still has to be refused.
 *
 * `event.source` cannot be set through `new MessageEvent`, so each
 * test defines it on the event object directly. That is the same
 * thing the browser does, and it is what lets a test send a message
 * that claims to be from a different window.
 */
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ExtensionFrame } from "./ExtensionFrame";
import { BRIDGE_VERSION, methodAllowed } from "./protocol";

const ORIGIN = "http://127.0.0.1:54321";

/** The reply the host posted into the frame, if it posted one. */
function repliesTo(frame: HTMLIFrameElement): unknown[] {
  const post = frame.contentWindow?.postMessage as unknown as {
    mock?: { calls: unknown[][] };
  };
  return (post?.mock?.calls ?? []).map((call) => call[0]);
}

function mount(over: Partial<Parameters<typeof ExtensionFrame>[0]> = {}) {
  const onCall = vi.fn(async () => ({ ok: true }));
  const onNavigate = vi.fn();
  const onClose = vi.fn();
  // Through the render's own container, not ``document.querySelector``:
  // a second mount in one test would otherwise hand back the first
  // one's iframe, and every assertion would be about the wrong frame.
  const { container, rerender } = render(
    <ExtensionFrame
      extensionId="igni.codeindex"
      origin={ORIGIN}
      allow={["codeindex_status", "codeindex_sync_*"]}
      onCall={onCall}
      onNavigate={onNavigate}
      onClose={onClose}
      {...over}
    />,
  );
  const frame = container.querySelector("iframe") as HTMLIFrameElement;
  // jsdom gives the iframe a real contentWindow; replacing postMessage
  // lets the test read what the host sent without a second document.
  Object.defineProperty(frame.contentWindow!, "postMessage", {
    value: vi.fn(),
    writable: true,
  });
  return { frame, onCall, onNavigate, onClose, rerender };
}

/** The nonce the host issued, read out of its `hello`. */
function handshake(frame: HTMLIFrameElement): string {
  frame.dispatchEvent(new Event("load"));
  const hello = repliesTo(frame).find(
    (m) => (m as { kind?: string }).kind === "hello",
  ) as { nonce: string } | undefined;
  expect(hello, "the host should greet the frame on load").toBeDefined();
  return hello!.nonce;
}

function send(
  frame: HTMLIFrameElement,
  body: Record<string, unknown>,
  over: { origin?: string; source?: unknown } = {},
) {
  const event = new MessageEvent("message", {
    data: { igni: BRIDGE_VERSION, ...body },
    origin: over.origin ?? ORIGIN,
  });
  Object.defineProperty(event, "source", {
    value: "source" in over ? over.source : frame.contentWindow,
  });
  window.dispatchEvent(event);
}

beforeEach(() => {
  vi.restoreAllMocks();
});

// React trees accumulate in the document otherwise, and the window
// ``message`` listener of every frame mounted so far is still attached
// — so a later test's message reaches an earlier test's component.
afterEach(cleanup);

describe("the frame itself", () => {
  it("is sandboxed, and keeps the one permission it cannot work without", () => {
    const { frame } = mount();
    const sandbox = frame.getAttribute("sandbox") ?? "";

    // Measured in Chromium: without this the frame's own module script
    // is a cross-origin request and does not load, and every message
    // it sends arrives as origin "null".
    expect(sandbox).toContain("allow-same-origin");
    expect(sandbox).toContain("allow-scripts");
    // And the ones a panel has no business having.
    expect(sandbox).not.toContain("allow-top-navigation");
    expect(sandbox).not.toContain("allow-popups");
    expect(sandbox).not.toContain("allow-modals");
    expect(sandbox).not.toContain("allow-downloads");
  });

  it("grants no permission-policy features at all", () => {
    const { frame } = mount();
    expect(frame.getAttribute("allow")).toBeNull();
  });

  it("loads the extension's own origin", () => {
    const { frame } = mount();
    expect(frame.getAttribute("src")).toBe(`${ORIGIN}/index.html`);
  });
});

describe("what the host accepts", () => {
  it("forwards a granted call and returns its result", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    send(frame, {
      kind: "call",
      nonce,
      id: "1",
      method: "codeindex_status",
      args: {},
    });

    await waitFor(() =>
      expect(onCall).toHaveBeenCalledWith("codeindex_status", {}),
    );
    await waitFor(() =>
      expect(repliesTo(frame)).toContainEqual(
        expect.objectContaining({
          kind: "result",
          id: "1",
          ok: true,
          value: { ok: true },
        }),
      ),
    );
  });

  it("matches a prefix grant", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    send(frame, {
      kind: "call",
      nonce,
      id: "2",
      method: "codeindex_sync_start",
      args: {},
    });

    await waitFor(() =>
      expect(onCall).toHaveBeenCalledWith("codeindex_sync_start", {}),
    );
  });

  it("passes a failing call's reason back as a sentence", async () => {
    const onCall = vi
      .fn()
      .mockRejectedValue(new Error("the index is not built yet"));
    const { frame } = mount({ onCall });
    const nonce = handshake(frame);

    send(frame, {
      kind: "call",
      nonce,
      id: "3",
      method: "codeindex_status",
      args: {},
    });

    await waitFor(() =>
      expect(repliesTo(frame)).toContainEqual(
        expect.objectContaining({
          id: "3",
          ok: false,
          error: "the index is not built yet",
        }),
      ),
    );
  });

  it("relays navigate and close to the app", () => {
    const { frame, onNavigate, onClose } = mount();
    const nonce = handshake(frame);

    send(frame, { kind: "navigate", nonce, to: "artifacts" });
    send(frame, { kind: "close", nonce });

    expect(onNavigate).toHaveBeenCalledWith("artifacts");
    expect(onClose).toHaveBeenCalled();
  });
});

describe("what the host refuses", () => {
  it("refuses a method the extension was not granted, and says what to do", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    send(frame, {
      kind: "call",
      nonce,
      id: "4",
      method: "run_shell",
      args: {},
    });

    await waitFor(() =>
      expect(repliesTo(frame)).toContainEqual(
        expect.objectContaining({ id: "4", ok: false }),
      ),
    );
    const refusal = repliesTo(frame).find(
      (m) => (m as { id?: string }).id === "4",
    ) as { error: string };
    expect(refusal.error).toContain("run_shell");
    expect(refusal.error).toContain("permissions");
    expect(onCall).not.toHaveBeenCalled();
  });

  it("ignores a message from another origin", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    send(
      frame,
      { kind: "call", nonce, id: "5", method: "codeindex_status", args: {} },
      { origin: "http://127.0.0.1:9999" },
    );

    await new Promise((r) => setTimeout(r, 10));
    expect(onCall).not.toHaveBeenCalled();
  });

  it("ignores a message from another window on the same origin", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    send(
      frame,
      { kind: "call", nonce, id: "6", method: "codeindex_status", args: {} },
      { source: window },
    );

    await new Promise((r) => setTimeout(r, 10));
    expect(onCall).not.toHaveBeenCalled();
  });

  it("ignores a message carrying the wrong nonce", async () => {
    const { frame, onCall } = mount();
    handshake(frame);

    send(frame, {
      kind: "call",
      nonce: "not-the-one-we-issued",
      id: "7",
      method: "codeindex_status",
      args: {},
    });

    await new Promise((r) => setTimeout(r, 10));
    expect(onCall).not.toHaveBeenCalled();
  });

  it("drops a malformed message without answering it", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);
    const before = repliesTo(frame).length;

    send(frame, { kind: "call", nonce, id: "", method: "", args: null });
    send(frame, { kind: "wat", nonce });

    await new Promise((r) => setTimeout(r, 10));
    expect(onCall).not.toHaveBeenCalled();
    // Silence, not a refusal: a guest that can tell malformed from
    // unauthorised has learned something about the host.
    expect(repliesTo(frame).length).toBe(before);
  });

  it("ignores a message from a bridge version it does not speak", async () => {
    const { frame, onCall } = mount();
    const nonce = handshake(frame);

    const event = new MessageEvent("message", {
      data: {
        igni: 99,
        kind: "call",
        nonce,
        id: "8",
        method: "codeindex_status",
        args: {},
      },
      origin: ORIGIN,
    });
    Object.defineProperty(event, "source", { value: frame.contentWindow });
    window.dispatchEvent(event);

    await new Promise((r) => setTimeout(r, 10));
    expect(onCall).not.toHaveBeenCalled();
  });
});

describe("the grant pattern language", () => {
  it("matches exactly, or on a trailing star, and nothing else", () => {
    expect(methodAllowed("codeindex_status", ["codeindex_status"])).toBe(true);
    expect(methodAllowed("codeindex_status", ["codeindex_"])).toBe(false);
    expect(methodAllowed("codeindex_sync_start", ["codeindex_sync_*"])).toBe(
      true,
    );
    expect(methodAllowed("codeindex_status", ["codeindex_sync_*"])).toBe(false);
    // A star anywhere but the end is a literal, not a wildcard: the
    // list is read by whoever approves an install.
    expect(methodAllowed("run_shell", ["*_shell"])).toBe(false);
    expect(methodAllowed("anything", ["*"])).toBe(true);
  });
});

describe("the handshake", () => {
  it("greets the frame with its id, params and theme", () => {
    const { frame } = mount({ params: { tab: "browse" }, theme: "light" });
    frame.dispatchEvent(new Event("load"));

    expect(repliesTo(frame)).toContainEqual(
      expect.objectContaining({
        kind: "hello",
        extensionId: "igni.codeindex",
        params: { tab: "browse" },
        theme: "light",
      }),
    );
  });

  it("gives two frames two different nonces", () => {
    const first = mount();
    const second = mount();
    expect(handshake(first.frame)).not.toBe(handshake(second.frame));
  });

  it("does not post anything to the frame before it has loaded", () => {
    const { frame } = mount();
    const kinds = repliesTo(frame).map((m) => (m as { kind: string }).kind);
    expect(kinds).not.toContain("hello");
  });
});

describe("following the host", () => {
  it("sends the theme as a message rather than reloading the frame", () => {
    const { frame, rerender } = mount({ theme: "dark" });
    handshake(frame);
    const src = frame.getAttribute("src");

    rerender(
      <ExtensionFrame
        extensionId="igni.codeindex"
        origin={ORIGIN}
        allow={[]}
        onCall={vi.fn()}
        theme="light"
      />,
    );

    expect(repliesTo(frame)).toContainEqual(
      expect.objectContaining({ kind: "theme", theme: "light" }),
    );
    // And the src is untouched: a panel that reloaded when the user
    // flipped to light mode would lose whatever they were in the
    // middle of.
    expect(frame.getAttribute("src")).toBe(src);
  });
});
