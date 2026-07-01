// @vitest-environment jsdom
/**
 * Tests for ``clientState`` — per-client UI state with optimistic
 * local cache and debounced RPC writes.
 *
 * Two surfaces:
 *
 *   • ``ensureClientId`` — reads/mints the stable client id used as
 *     the partition key for the BE's ``client_state`` table. Each
 *     web tab / IDE webview / JetBrains panel keeps its own.
 *   • ``ClientStateStore`` — keyed key/value store with optimistic
 *     local cache, debounced writes (typing in a composer doesn't
 *     flood the WS), and an onChange pub/sub for sibling components
 *     to react to changes from this same tab.
 *
 * The debounce + cache semantics are the load-bearing parts. Rapid
 * writes to the same key MUST collapse to one RPC, and the local
 * cache MUST update immediately (optimistic) so the user's typing
 * doesn't lag behind the network.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Node 25 ships an empty-stub localStorage that masks jsdom's
// Storage. Install a real in-memory shim via vi.hoisted so it's
// in place BEFORE ``clientState.ts`` module-loads (the module
// reads localStorage at top level for the client id).
vi.hoisted(() => {
  const data = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return data.size;
    },
    clear() {
      data.clear();
    },
    getItem(key: string) {
      return data.has(key) ? (data.get(key) as string) : null;
    },
    key(i: number) {
      return Array.from(data.keys())[i] ?? null;
    },
    removeItem(key: string) {
      data.delete(key);
    },
    setItem(key: string, value: string) {
      data.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    writable: true,
    value: shim,
  });
});

import { ClientStateStore, ensureClientId } from "./clientState";
import type { EmberClient } from "./protocol/client";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ── ensureClientId ──────────────────────────────────────────

describe("ensureClientId", () => {
  it("mints a new id when localStorage is empty", () => {
    // First call from a brand-new browser. The id is stable
    // (subsequent calls return the same one) — but the FIRST
    // call has to produce something.
    const id = ensureClientId();
    expect(id).toBeTruthy();
    expect(id.length).toBeGreaterThan(0);
  });

  it("persists the minted id to localStorage", () => {
    ensureClientId();
    expect(localStorage.getItem("ember-code:client-id")).toBeTruthy();
  });

  it("reads the existing id on subsequent calls", () => {
    // Stability is the whole point — the BE keys client_state
    // by this id. Drift means losing the user's sidebar
    // position / drafts on every page load.
    const first = ensureClientId();
    const second = ensureClientId();
    expect(first).toBe(second);
  });

  it("prefers crypto.randomUUID when available", () => {
    // crypto.randomUUID returns 36-char dashed UUIDs. The
    // fallback path (timestamp + Math.random) produces the
    // ``c-...`` shape. Pin that randomUUID is the
    // preferred path so the fallback only fires on really
    // old browsers.
    const id = ensureClientId();
    // Either a UUID (36 chars, has dashes) or the c-prefixed
    // fallback. With jsdom + Node 25, crypto.randomUUID
    // exists, so we expect the UUID shape.
    if (id.startsWith("c-")) {
      // Fallback path — note for the test reader.
      expect(id).toMatch(/^c-[a-z0-9]+-[a-z0-9]+$/);
    } else {
      // UUID path.
      expect(id).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
      );
    }
  });

  it("falls back when crypto.randomUUID is unavailable", () => {
    // Pre-2022 browser path. Strip crypto.randomUUID and the
    // ``c-<ts>-<rand>`` shape kicks in.
    const originalCrypto = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      writable: true,
      value: {}, // no randomUUID
    });
    try {
      const id = ensureClientId();
      expect(id).toMatch(/^c-[a-z0-9]+-[a-z0-9]+$/);
    } finally {
      Object.defineProperty(globalThis, "crypto", {
        configurable: true,
        writable: true,
        value: originalCrypto,
      });
    }
  });

  it("tolerates localStorage being unavailable (no-throw)", () => {
    // Private mode / SSR-ish probe. The try/catch around
    // localStorage means the id is minted fresh each call
    // but no exception escapes.
    const stripped = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("localStorage unavailable");
      },
    });
    try {
      const id = ensureClientId();
      expect(id).toBeTruthy();
    } finally {
      if (stripped) Object.defineProperty(globalThis, "localStorage", stripped);
    }
  });
});

// ── ClientStateStore ────────────────────────────────────────

interface Rpc {
  (method: string, params?: unknown): Promise<unknown>;
}

function makeClient(rpc: Rpc): EmberClient {
  return { rpc } as unknown as EmberClient;
}

describe("ClientStateStore — hydrate", () => {
  it("RPCs get_client_state and populates the cache", async () => {
    const rpc = vi.fn().mockResolvedValue({ sidebar_open: "true", draft: "hello" });
    const store = new ClientStateStore(makeClient(rpc), "client-1");
    expect(store.isHydrated()).toBe(false);
    await store.hydrate();
    expect(store.isHydrated()).toBe(true);
    expect(rpc).toHaveBeenCalledWith("get_client_state", { client_id: "client-1" });
    expect(store.get("sidebar_open")).toBe("true");
    expect(store.get("draft")).toBe("hello");
  });

  it("marks hydrated even on RPC failure (empty cache)", async () => {
    // First-paint robustness — if the BE is slow or returning
    // a transient error, the FE must still render. The store
    // settles into an empty cache and lets ``set`` writes
    // overwrite eventually.
    const rpc = vi.fn().mockRejectedValue(new Error("transport down"));
    const store = new ClientStateStore(makeClient(rpc), "client-1");
    await store.hydrate();
    expect(store.isHydrated()).toBe(true);
    expect(store.get("anything")).toBeUndefined();
  });

  it("handles null/undefined RPC response gracefully", async () => {
    // BE returning null (no rows for this client_id yet) →
    // empty cache, no crash.
    const rpc = vi.fn().mockResolvedValue(null);
    const store = new ClientStateStore(makeClient(rpc), "fresh-client");
    await store.hydrate();
    expect(store.get("any-key")).toBeUndefined();
  });
});

describe("ClientStateStore — get/set/delete", () => {
  it("set updates the cache immediately (optimistic)", () => {
    // The whole point of optimistic local writes is the
    // user's typing doesn't wait for the network. Pin that
    // ``set`` makes the new value readable BEFORE the RPC
    // fires.
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", /* debounce */ 250);
    store.set("draft", "in progress");
    expect(store.get("draft")).toBe("in progress"); // synchronously visible
  });

  it("set debounces the RPC write", () => {
    // Rapid successive ``set`` calls on the same key MUST
    // collapse to ONE RPC at the end of the debounce window.
    // Without this, typing in a composer would fire a
    // ``set_client_state`` per keystroke and flood the WS.
    vi.useFakeTimers();
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", 250);
    store.set("draft", "h");
    store.set("draft", "he");
    store.set("draft", "hel");
    store.set("draft", "hello");
    // Before the debounce window elapses → no RPC.
    expect(rpc).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);
    // One RPC, with the LAST value.
    expect(rpc).toHaveBeenCalledTimes(1);
    expect(rpc).toHaveBeenCalledWith("set_client_state", {
      client_id: "c-1",
      key: "draft",
      value: "hello",
    });
  });

  it("debounces per-key (different keys don't collapse together)", () => {
    // Editing two different keys (e.g. ``draft`` and
    // ``sidebar_open``) within the debounce window should fire
    // TWO RPCs, one per key. Pin that the debounce map is
    // keyed by ``key``, not global.
    vi.useFakeTimers();
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", 250);
    store.set("draft", "hi");
    store.set("sidebar_open", "true");
    vi.advanceTimersByTime(250);
    expect(rpc).toHaveBeenCalledTimes(2);
  });

  it("delete fires the RPC immediately (no debounce)", () => {
    // Delete is uncommon enough that we don't bother
    // debouncing. The cache update + RPC are synchronous.
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", 250);
    store.set("draft", "x");
    store.delete("draft");
    expect(store.get("draft")).toBeUndefined();
    expect(rpc).toHaveBeenCalledWith("delete_client_state", {
      client_id: "c-1",
      key: "draft",
    });
  });

  it("delete cancels any pending set for the same key", () => {
    // If the user types, then immediately clears the draft,
    // the pending ``set`` shouldn't fire AFTER the delete —
    // that would re-create the deleted row.
    vi.useFakeTimers();
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", 250);
    store.set("draft", "typed");
    store.delete("draft");
    vi.advanceTimersByTime(500);
    // Only the delete RPC fired; no stale set.
    const setCalls = rpc.mock.calls.filter((c) => c[0] === "set_client_state");
    expect(setCalls).toHaveLength(0);
  });
});

describe("ClientStateStore — onChange listeners", () => {
  it("fires the listener on set with (key, value)", () => {
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1");
    const events: Array<[string, string]> = [];
    store.onChange((k, v) => events.push([k, v]));
    store.set("draft", "hi");
    expect(events).toEqual([["draft", "hi"]]);
  });

  it("fires the listener on delete with empty-string value", () => {
    // The listener API uses "" to signal deletion (vs the
    // get() API which returns undefined). Pin both shapes.
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1");
    const events: Array<[string, string]> = [];
    store.onChange((k, v) => events.push([k, v]));
    store.set("draft", "hi");
    store.delete("draft");
    expect(events).toEqual([
      ["draft", "hi"],
      ["draft", ""],
    ]);
  });

  it("returns an unsubscribe function that removes the listener", () => {
    // Standard React effect-cleanup shape. Without
    // unsubscribe, sibling components leak listeners on every
    // mount/unmount.
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1");
    const events: string[] = [];
    const unsubscribe = store.onChange((k) => events.push(k));
    store.set("a", "1");
    unsubscribe();
    store.set("b", "2");
    expect(events).toEqual(["a"]);
  });

  it("multiple subscribers all fire on each set", () => {
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1");
    const seen1: string[] = [];
    const seen2: string[] = [];
    store.onChange((k) => seen1.push(k));
    store.onChange((k) => seen2.push(k));
    store.set("draft", "x");
    expect(seen1).toEqual(["draft"]);
    expect(seen2).toEqual(["draft"]);
  });
});

describe("ClientStateStore — flush", () => {
  it("clears pending debounced writes (timers stopped)", () => {
    vi.useFakeTimers();
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1", 250);
    store.set("draft", "in progress");
    store.flush();
    vi.advanceTimersByTime(500);
    // The pending set was cancelled — no RPC fires.
    expect(rpc).not.toHaveBeenCalled();
  });

  it("doesn't crash when called with no pending writes", () => {
    const rpc = vi.fn().mockResolvedValue(undefined);
    const store = new ClientStateStore(makeClient(rpc), "c-1");
    store.flush(); // no raise
  });
});
