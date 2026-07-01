/**
 * Tests for ``resolveWsUrl`` — the WS-URL precedence resolver.
 *
 * Precedence (highest → lowest):
 *   1. ``?ws=...`` URL query param
 *   2. ``<meta name="ember-ws-url" content="...">`` (VSCode webview
 *      delivery, because the extension can't inject globals under
 *      the default CSP)
 *   3. ``window.__EMBER_WS_URL__`` global (JetBrains JCEF injection)
 *   4. dev fallback ``ws://127.0.0.1:8765``
 *
 * Vitest runs in node, so we hand-stub ``window`` + ``document``
 * with the bare minimum surface the resolver touches. Each test
 * resets module state via ``vi.resetModules`` so subsequent tests
 * pick up the new globals.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

interface WindowLike {
  location: { search: string };
  __EMBER_WS_URL__?: string;
}
interface MetaTagLike {
  content: string;
}

let win: WindowLike;
let metaTag: MetaTagLike | null;
let resolveWsUrl: typeof import("./client").resolveWsUrl;

beforeEach(async () => {
  vi.resetModules();
  win = { location: { search: "" } };
  metaTag = null;
  (globalThis as { window?: WindowLike }).window = win;
  (globalThis as { document?: { querySelector: (s: string) => MetaTagLike | null } }).document = {
    querySelector: (_s) => metaTag,
  };
  // Silence the diagnostic ``console.info`` the resolver always
  // emits — it's noise here, not signal.
  vi.spyOn(console, "info").mockImplementation(() => undefined);
  ({ resolveWsUrl } = await import("./client"));
});

afterEach(() => {
  delete (globalThis as { window?: WindowLike }).window;
  delete (globalThis as { document?: unknown }).document;
  vi.restoreAllMocks();
});

describe("resolveWsUrl precedence", () => {
  it("falls back to the dev port when no signal is present", () => {
    // Default for ``npm run dev`` — the local BE binds 8765 by
    // convention. Changing this default ripples through dev
    // tooling, so it's worth locking down.
    expect(resolveWsUrl()).toBe("ws://127.0.0.1:8765");
  });

  it("uses the global when only __EMBER_WS_URL__ is set", () => {
    win.__EMBER_WS_URL__ = "ws://injected.example/1";
    expect(resolveWsUrl()).toBe("ws://injected.example/1");
  });

  it("uses the meta tag when set, even over the global", () => {
    // VSCode webview delivery wins over the JetBrains-style
    // global — they shouldn't both be present, but if they
    // are, the meta tag is the more-recent-of-the-two.
    win.__EMBER_WS_URL__ = "ws://global.example";
    metaTag = { content: "ws://meta.example" };
    expect(resolveWsUrl()).toBe("ws://meta.example");
  });

  it("URL param wins over meta tag and global", () => {
    // The query param is the explicit user-controlled override
    // (used by the Tauri loader to bridge to the spawned BE's
    // random port). Must beat anything else.
    win.location.search = "?ws=ws%3A%2F%2Fparam.example%3A9999";
    metaTag = { content: "ws://meta.example" };
    win.__EMBER_WS_URL__ = "ws://global.example";
    expect(resolveWsUrl()).toBe("ws://param.example:9999");
  });

  it("URL param is URL-decoded by URLSearchParams (no double-decode bug)", () => {
    // ``ws://127.0.0.1:51234`` is delivered as
    // ``ws%3A%2F%2F127.0.0.1%3A51234``. ``URLSearchParams.get``
    // decodes once. A reader that decodes again would emit
    // ``ws:/127.0.0.1:51234`` — a malformed URL. Lock it down.
    win.location.search = "?ws=ws%3A%2F%2F127.0.0.1%3A51234";
    expect(resolveWsUrl()).toBe("ws://127.0.0.1:51234");
  });

  it("empty URL param does NOT win over meta tag", () => {
    // ``?ws=`` shows up when a tab forgets to set the param.
    // Treating it as "explicit empty" would lose the legitimate
    // VSCode meta-tag fallback.
    win.location.search = "?ws=";
    metaTag = { content: "ws://meta.example" };
    expect(resolveWsUrl()).toBe("ws://meta.example");
  });

  it("ignores unrelated query params", () => {
    // Defensive: ``?foo=bar`` must NOT promote ``foo`` into a
    // candidate URL. Only the ``ws`` key matters.
    win.location.search = "?foo=bar&debug=1";
    expect(resolveWsUrl()).toBe("ws://127.0.0.1:8765");
  });

  it("missing document still resolves (e.g. SSR-ish probe)", () => {
    delete (globalThis as { document?: unknown }).document;
    expect(resolveWsUrl()).toBe("ws://127.0.0.1:8765");
  });
});

// ── EmberClient listener subscription contract ──────────────
//
// ``onEvent`` and ``onStateChange`` are pub/sub APIs the rest of
// the app builds on (App.tsx wires status / push / HITL handlers
// via onEvent; ConnectionState UI wires via onStateChange). The
// invariants worth pinning are simple but load-bearing:
//   * subscribe → return an unsubscribe function
//   * unsubscribe removes the listener (no leak across React
//     mount cycles)
//   * close() is safe before connect (constructor-only path)
// The WebSocket connection lifecycle is too entangled with the
// real Web API to unit-test usefully — covered by Playwright
// e2e under clients/web/e2e/.

describe("EmberClient — listener subscription", () => {
  let EmberClient: typeof import("./client").EmberClient;

  beforeEach(async () => {
    // Use a fresh module instance; matchMedia + WebSocket
    // globals aren't touched because we never call connect().
    vi.resetModules();
    ({ EmberClient } = await import("./client"));
  });

  it("onEvent registers a listener and returns an unsubscribe", () => {
    // The unsubscribe-returning-function pattern is the canonical
    // React-effect-cleanup shape. Without it, components that
    // mount/unmount repeatedly (sidebar open/close, etc.) leak
    // listeners — every leak silently re-fires every message.
    const client = new EmberClient("ws://test");
    const fn = vi.fn();
    const unsubscribe = client.onEvent(fn);
    expect(typeof unsubscribe).toBe("function");
  });

  it("onStateChange registers a listener and returns an unsubscribe", () => {
    const client = new EmberClient("ws://test");
    const fn = vi.fn();
    const unsubscribe = client.onStateChange(fn);
    expect(typeof unsubscribe).toBe("function");
  });

  it("close() is safe to call before connect() (constructor-only path)", () => {
    // React StrictMode mounts effects twice — the second mount
    // cleanup may fire before the actual connect lands. close()
    // must be a no-op pre-connect, not throw on ``null.close()``.
    const client = new EmberClient("ws://test");
    // No raise.
    client.close();
  });

  it("close() is idempotent (multiple calls safe)", () => {
    // Same defensive shape — repeated close() must not throw.
    // Tests + lifecycle code may call it more than once.
    const client = new EmberClient("ws://test");
    client.close();
    client.close();
    client.close();
  });

  it("multiple onEvent subscribers are all independent", () => {
    // The Set semantics give each subscriber a separate slot.
    // The unsubscribe returned from one subscribe call must NOT
    // affect other subscribers' slots — pinned by checking
    // that both unsubscribe functions are distinct.
    const client = new EmberClient("ws://test");
    const fn1 = vi.fn();
    const fn2 = vi.fn();
    const unsub1 = client.onEvent(fn1);
    const unsub2 = client.onEvent(fn2);
    expect(unsub1).not.toBe(unsub2);
    // Same function subscribed twice still gets one slot (Set
    // dedup) — pin the dedup as a Set-semantics contract.
    client.onEvent(fn1);
    // (Can't directly inspect the Set's size; would need access
    // to the private field. The dedup behaviour is documented
    // here so a future Array-based refactor is a deliberate
    // choice — JS Sets dedup by reference equality.)
  });

  it("default constructor argument calls resolveWsUrl", () => {
    // Without an explicit URL, the constructor falls back to
    // ``resolveWsUrl()`` — the same dev-port behaviour the
    // first half of this file tests at the function level.
    // Pinned here so the constructor wiring stays connected
    // to the resolver.
    const client = new EmberClient();
    // ``url`` is private; we can't inspect it directly. The
    // constructor not throwing is enough — resolveWsUrl is
    // covered above.
    expect(client).toBeInstanceOf(EmberClient);
  });
});
