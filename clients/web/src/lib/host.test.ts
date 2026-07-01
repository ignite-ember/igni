/**
 * Tests for the host-capability layer — specifically the
 * indexed-search bridge (CC parity row 44). The shape:
 *
 *   • JetBrains (kind="jetbrains") → window.cefQuery
 *   • VSCode    (kind="vscode")    → acquireVsCodeApi + postMessage
 *   • everything else              → null (caller falls back to WS RPC)
 *
 * Vitest's default env is "node" in this project (no jsdom). We
 * stub the bits of ``window`` that ``host.ts`` actually touches —
 * cefQuery, acquireVsCodeApi, setTimeout/clearTimeout (real ones
 * from Node), and a minimal event-listener registry for the VSCode
 * postMessage round-trip.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type CefQueryOpts = {
  request: string;
  onSuccess?: (r: string) => void;
  onFailure?: (code: number, msg: string) => void;
};
type CefQueryFn = (opts: CefQueryOpts) => void;
type VsApi = { postMessage: (m: unknown) => void };

interface WindowLike {
  cefQuery?: CefQueryFn;
  acquireVsCodeApi?: () => VsApi;
  __TAURI__?: unknown;
  __TAURI_INTERNALS__?: unknown;
  __EMBER_HOST__?: {
    openFile?: (p: string) => unknown;
    notify?: (payload: { title: string; body?: string }) => unknown;
  };
  setTimeout: typeof setTimeout;
  clearTimeout: typeof clearTimeout;
  addEventListener: (
    type: string,
    fn: (ev: { data: unknown }) => void,
    opts?: { once?: boolean },
  ) => void;
  removeEventListener: (type: string, fn: (ev: { data: unknown }) => void) => void;
  postMessage: (data: unknown, target?: string) => void;
}

// One shared event-listener registry — VSCode replies dispatched via
// ``win.postMessage`` reach every "message" listener, matching the
// browser semantics ``host.searchCode`` relies on.
function makeWindow(): WindowLike {
  const listeners: { type: string; fn: (ev: { data: unknown }) => void; once?: boolean }[] = [];
  return {
    setTimeout,
    clearTimeout,
    addEventListener(type, fn, opts) {
      listeners.push({ type, fn, once: opts?.once });
    },
    removeEventListener(type, fn) {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    postMessage(data) {
      // Copy the snapshot so once-listeners can splice without
      // skipping subsequent same-tick listeners.
      const snap = listeners.slice();
      for (const l of snap) {
        if (l.type !== "message") continue;
        l.fn({ data });
        if (l.once) {
          const i = listeners.indexOf(l);
          if (i >= 0) listeners.splice(i, 1);
        }
      }
    },
  };
}

let win: WindowLike;
let HostMod: typeof import("./host");

beforeEach(async () => {
  // Reset modules each test so the ``cachedVsCodeApi`` module-local
  // doesn't leak between cases (one test stubs the api, the next
  // tries with no api — without the reset the cache would hand back
  // the prior stub).
  vi.resetModules();
  win = makeWindow();
  (globalThis as { window?: WindowLike }).window = win;
  HostMod = await import("./host");
});

afterEach(() => {
  delete (globalThis as { window?: WindowLike }).window;
});

// ── Short-circuits applied to every host kind ─────────────

describe("Host.searchCode short-circuits", () => {
  it("returns null when snippet is empty", async () => {
    const host = new HostMod.Host();
    expect(await host.searchCode("")).toBeNull();
  });

  it("returns null when snippet < 5 chars", async () => {
    // The threshold is "don't bother indexing tiny strings —
    // the false-positive rate dominates and the user usually
    // didn't intend to search". 5 is the documented floor.
    const host = new HostMod.Host();
    expect(await host.searchCode("abcd")).toBeNull();
  });

  it("returns null on plain web (no native bridge)", async () => {
    // No cefQuery, no acquireVsCodeApi → falls through to the
    // ``return null`` at the end of ``searchCode``. Caller
    // (Composer) falls back to the WS ``search_code`` RPC.
    const host = new HostMod.Host();
    expect(host.kind).toBe("web");
    expect(await host.searchCode("a long enough snippet")).toBeNull();
  });
});

// ── JetBrains branch (cefQuery) ──────────────────────────

describe("Host.searchCode — JetBrains", () => {
  it("dispatches the search via cefQuery and resolves with parsed JSON", async () => {
    const result = {
      matches: [{ path: "src/x.py", line: 42, preview: "match line" }],
      truncated: false,
    };
    let receivedRequest: string | undefined;
    win.cefQuery = vi.fn((opts: CefQueryOpts) => {
      receivedRequest = opts.request;
      opts.onSuccess?.(JSON.stringify(result));
    }) as CefQueryFn;

    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    const out = await host.searchCode("symbol name");
    expect(out).toEqual(result);
    // Request is the documented ``ember:searchCode`` shape so the
    // native bridge can dispatch by type.
    expect(receivedRequest).toBeDefined();
    const parsed = JSON.parse(receivedRequest!) as { type: string; snippet: string };
    expect(parsed.type).toBe("ember:searchCode");
    expect(parsed.snippet).toBe("symbol name");
  });

  it("returns null when onFailure fires", async () => {
    win.cefQuery = vi.fn((opts: CefQueryOpts) => {
      opts.onFailure?.(500, "boom");
    }) as CefQueryFn;

    const host = new HostMod.Host();
    expect(await host.searchCode("symbol name")).toBeNull();
  });

  it("returns null on malformed JSON in onSuccess", async () => {
    // The native bridge can send arbitrary strings — a parse
    // failure must NOT propagate as an exception, it should
    // degrade to "no native search" so the WS fallback fires.
    win.cefQuery = vi.fn((opts: CefQueryOpts) => {
      opts.onSuccess?.("{not valid json");
    }) as CefQueryFn;

    const host = new HostMod.Host();
    expect(await host.searchCode("symbol name")).toBeNull();
  });

  it("returns null when cefQuery itself throws", async () => {
    win.cefQuery = vi.fn(() => {
      throw new Error("bridge broken");
    }) as CefQueryFn;

    const host = new HostMod.Host();
    expect(await host.searchCode("symbol name")).toBeNull();
  });

  it("only resolves once even when the bridge fires success twice", async () => {
    // Defensive: if the bridge calls ``onSuccess`` more than once
    // (some buggy builds did this), the second call must be a
    // no-op — otherwise the promise resolves twice and downstream
    // code sees wonky results.
    win.cefQuery = vi.fn((opts: CefQueryOpts) => {
      opts.onSuccess?.(JSON.stringify({ matches: [], truncated: false }));
      opts.onSuccess?.(JSON.stringify({ matches: ["never"], truncated: true }));
      opts.onFailure?.(500, "late failure");
    }) as CefQueryFn;

    const host = new HostMod.Host();
    const out = await host.searchCode("symbol name");
    expect(out).toEqual({ matches: [], truncated: false });
  });

  it("times out at 3s when the bridge never replies", async () => {
    // Reach for fake timers ONLY here — using them globally would
    // freeze the actual setTimeout/clearTimeout that ``win`` proxies
    // out to Node's globals.
    vi.useFakeTimers();
    try {
      win.cefQuery = vi.fn((_opts: CefQueryOpts) => {
        // Black hole — no callback fires.
      }) as CefQueryFn;
      // The window stub captured the REAL setTimeout at construction;
      // vi.useFakeTimers swaps the global one but not the captured
      // reference. Re-assign so the in-flight promise picks up fakes.
      win.setTimeout = globalThis.setTimeout;
      win.clearTimeout = globalThis.clearTimeout;

      const host = new HostMod.Host();
      const p = host.searchCode("symbol name");
      vi.advanceTimersByTime(3001);
      expect(await p).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("returns null when cefQuery is missing but __EMBER_HOST__ marks JetBrains", async () => {
    // Edge case — detectHost picked jetbrains because
    // __EMBER_HOST__.openFile is set, but cefQuery isn't on the
    // window (some JCEF setups inject one without the other).
    win.__EMBER_HOST__ = { openFile: () => undefined };

    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    expect(await host.searchCode("symbol name")).toBeNull();
  });
});

// ── VSCode branch (postMessage) ──────────────────────────

describe("Host.searchCode — VSCode", () => {
  it("posts a request with a unique id and resolves on matching reply", async () => {
    let postedId: unknown;
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        const m = msg as { type: string; id: number; snippet: string };
        expect(m.type).toBe("ember:searchCode");
        expect(m.snippet).toBe("vscode snippet");
        postedId = m.id;
        win.postMessage({
          type: "ember:searchCodeResult",
          id: m.id,
          result: { matches: [{ path: "a", line: 1, preview: "p" }], truncated: false },
        });
      },
    });

    const host = new HostMod.Host();
    expect(host.kind).toBe("vscode");
    const out = await host.searchCode("vscode snippet");
    expect(postedId).toBeDefined();
    expect(out).toEqual({
      matches: [{ path: "a", line: 1, preview: "p" }],
      truncated: false,
    });
  });

  it("ignores replies with mismatched id", async () => {
    // The id-routing logic exists specifically because an early
    // build crossed concurrent searches when the user typed quickly
    // — a stale reply would resolve the newer promise with old data.
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        const m = msg as { type: string; id: number };
        // First send a reply with a WRONG id — must be ignored.
        win.postMessage({
          type: "ember:searchCodeResult",
          id: m.id + 999,
          result: { matches: ["wrong"], truncated: false },
        });
        // Then the correct one.
        win.postMessage({
          type: "ember:searchCodeResult",
          id: m.id,
          result: { matches: ["right"], truncated: true },
        });
      },
    });

    const host = new HostMod.Host();
    const out = await host.searchCode("vscode snippet");
    expect(out).toEqual({ matches: ["right"], truncated: true });
  });

  it("ignores replies of the wrong type", async () => {
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        const m = msg as { id: number };
        win.postMessage({ type: "ember:somethingElse", id: m.id, result: "no" });
        win.postMessage({ type: "ember:searchCodeResult", id: m.id, result: "yes" });
      },
    });

    const host = new HostMod.Host();
    const out = await host.searchCode("vscode snippet");
    expect(out).toBe("yes");
  });

  it("times out at 3s when no reply arrives", async () => {
    vi.useFakeTimers();
    try {
      win.acquireVsCodeApi = () => ({
        postMessage: (_msg: unknown) => {
          // No reply — promise must time out.
        },
      });
      win.setTimeout = globalThis.setTimeout;
      win.clearTimeout = globalThis.clearTimeout;

      const host = new HostMod.Host();
      const p = host.searchCode("vscode snippet");
      vi.advanceTimersByTime(3001);
      expect(await p).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

// ── Default branch (web / tauri / unknown) ──────────────

describe("Host.searchCode — non-IDE hosts", () => {
  it("returns null on tauri", async () => {
    win.__TAURI__ = {};
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    expect(await host.searchCode("a long snippet")).toBeNull();
  });

  it("returns null on plain web", async () => {
    const host = new HostMod.Host();
    expect(host.kind).toBe("web");
    expect(await host.searchCode("a long snippet")).toBeNull();
  });
});

// ── Host.notify — OS / IDE notification dispatch ────────────
//
// The notify() bridge surfaces project-level async events
// (scheduled task finished, etc.) as native OS / IDE banners
// when the host has the wiring; falls back to the FE's in-app
// toast otherwise. The bridge.notify() callback wins over the
// per-host branch when present (the host injected a richer API).

describe("Host.notify", () => {
  it("returns false when payload is empty (no title or body)", async () => {
    // Don't fire blank notifications. The truthy-title-or-body
    // guard prevents the OS from showing an empty banner.
    const host = new HostMod.Host();
    expect(await host.notify({ title: "", body: "" })).toBe(false);
    expect(await host.notify({ title: "" })).toBe(false);
  });

  it("returns false on plain web (no native bridge)", async () => {
    // Web falls through every branch and returns false — the
    // caller falls back to the in-app toast.
    const host = new HostMod.Host();
    expect(await host.notify({ title: "Task done" })).toBe(false);
  });

  it("dispatches via __EMBER_HOST__.notify when bridge is wired", async () => {
    // The bridge takes precedence over the per-host branch
    // — host-injected APIs (JetBrains, future Tauri shim)
    // tend to support richer notifications than the generic
    // plugin paths.
    const bridgeNotify = vi.fn().mockResolvedValue(undefined);
    win.__EMBER_HOST__ = { notify: bridgeNotify };
    const host = new HostMod.Host();
    const result = await host.notify({ title: "T", body: "B" });
    expect(result).toBe(true);
    expect(bridgeNotify).toHaveBeenCalledWith({ title: "T", body: "B" });
  });

  it("Tauri host dispatches via plugin:notification|notify", async () => {
    // Native banner via the Tauri notification plugin.
    const invoke = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = { core: { invoke } };
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    const result = await host.notify({ title: "T", body: "B" });
    expect(result).toBe(true);
    expect(invoke).toHaveBeenCalledWith("plugin:notification|notify", {
      options: { title: "T", body: "B" },
    });
  });

  it("Tauri body defaults to empty string when omitted", async () => {
    // The Tauri plugin requires the ``body`` key. Without
    // the ``|| ""`` fallback the invoke would carry
    // ``body: undefined``.
    const invoke = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = { core: { invoke } };
    const host = new HostMod.Host();
    await host.notify({ title: "T" });
    expect(invoke).toHaveBeenCalledWith("plugin:notification|notify", {
      options: { title: "T", body: "" },
    });
  });

  it("Tauri without core.invoke falls through to false (incomplete shim)", async () => {
    // Defensive — ``window.__TAURI__`` may be a partial shim
    // (early Tauri 2 versions, or a dev override). Don't
    // crash, just return false so the FE shows the in-app
    // toast.
    win.__TAURI__ = {}; // no core.invoke
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    expect(await host.notify({ title: "T" })).toBe(false);
  });

  it("VSCode host dispatches via postMessage ember:notify", async () => {
    let posted: unknown = null;
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        posted = msg;
      },
    });
    const host = new HostMod.Host();
    expect(host.kind).toBe("vscode");
    const result = await host.notify({ title: "T", body: "B" });
    expect(result).toBe(true);
    expect(posted).toMatchObject({ type: "ember:notify", title: "T", body: "B" });
  });

  it("JetBrains host dispatches via cefQuery", async () => {
    let request: string | undefined;
    win.cefQuery = ((opts: { request: string }) => {
      request = opts.request;
    }) as CefQueryFn;
    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    const result = await host.notify({ title: "T", body: "B" });
    expect(result).toBe(true);
    const parsed = JSON.parse(request!) as { type: string; title: string };
    expect(parsed.type).toBe("ember:notify");
    expect(parsed.title).toBe("T");
  });

  it("swallows exceptions from the dispatch and returns false", async () => {
    // The notify wrapper is wrapped in a try/catch with
    // console.warn — must not propagate. Otherwise a buggy
    // bridge would crash the FE caller.
    const bridgeNotify = vi.fn().mockRejectedValue(new Error("boom"));
    win.__EMBER_HOST__ = { notify: bridgeNotify };
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const host = new HostMod.Host();
    expect(await host.notify({ title: "T" })).toBe(false);
    consoleWarn.mockRestore();
  });
});

// ── Host.notifyFileEdited — VFS refresh hint ────────────────
//
// Fired by the FE after the BE writes a file. JetBrains
// triggers a VFS refresh; VSCode reverts the open buffer;
// Tauri / web are no-ops (the IDE's own file watcher catches
// the change eventually).

describe("Host.notifyFileEdited", () => {
  it("is a no-op for empty path", async () => {
    // Defensive — avoid sending an empty ``path`` message
    // that the IDE would refuse anyway.
    const cefQuery = vi.fn();
    win.cefQuery = cefQuery as CefQueryFn;
    const host = new HostMod.Host();
    await host.notifyFileEdited("");
    expect(cefQuery).not.toHaveBeenCalled();
  });

  it("JetBrains: dispatches ember:fileEdited via cefQuery", async () => {
    let request: string | undefined;
    win.cefQuery = ((opts: { request: string }) => {
      request = opts.request;
    }) as CefQueryFn;
    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    await host.notifyFileEdited("/abs/file.py");
    const parsed = JSON.parse(request!) as { type: string; path: string };
    expect(parsed.type).toBe("ember:fileEdited");
    expect(parsed.path).toBe("/abs/file.py");
  });

  it("VSCode: dispatches ember:fileEdited via postMessage", async () => {
    let posted: unknown = null;
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        posted = msg;
      },
    });
    const host = new HostMod.Host();
    expect(host.kind).toBe("vscode");
    await host.notifyFileEdited("/abs/file.ts");
    expect(posted).toMatchObject({
      type: "ember:fileEdited",
      path: "/abs/file.ts",
    });
  });

  it("Tauri / web are no-ops (no IDE editor to refresh)", async () => {
    // The IDE's own file watcher picks up the change.
    // Calling these on tauri / web must not throw, but
    // there's nothing to assert on the message side.
    win.__TAURI__ = {};
    const tauriHost = new HostMod.Host();
    await tauriHost.notifyFileEdited("/x.py"); // no raise
    delete win.__TAURI__;
    const webHost = new HostMod.Host();
    await webHost.notifyFileEdited("/x.py"); // no raise
  });

  it("swallows exceptions (best-effort hint)", async () => {
    // notifyFileEdited is fire-and-forget — if the bridge
    // throws, the FE caller should not crash. The console.warn
    // is the only side effect.
    win.cefQuery = (() => {
      throw new Error("bridge broken");
    }) as CefQueryFn;
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const host = new HostMod.Host();
    // No raise — the catch in the source absorbs it.
    await host.notifyFileEdited("/x.py");
    consoleWarn.mockRestore();
  });
});

// ── Host.openFile — primary file-open routing ───────────────
//
// Called every time the user clicks a file pill, attachment,
// or file mention. Each host has its own native open path; the
// generic web case falls back to an in-app preview callback the
// FE registers at boot. Returns true when the open attempt was
// dispatched, false when only the fallback fired (or nothing
// could happen at all).

describe("Host.openFile", () => {
  it("returns false for an empty path (defensive)", async () => {
    // The composer occasionally builds a pill with an empty
    // path during drag-cancel races. Don't fire any bridge.
    const host = new HostMod.Host();
    expect(await host.openFile("")).toBe(false);
  });

  it("Tauri: uses __EMBER_HOST__.openFile when wired", async () => {
    // Bridge first — Tauri shells inject a richer
    // ``__EMBER_HOST__.openFile`` that knows about the
    // user's project root.
    const bridgeOpen = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = {};
    win.__EMBER_HOST__ = { openFile: bridgeOpen };
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    const result = await host.openFile("/abs/file.py");
    expect(result).toBe(true);
    expect(bridgeOpen).toHaveBeenCalledWith("/abs/file.py");
  });

  it("Tauri: falls back to plugin:shell|open when no bridge", async () => {
    // ``__TAURI__`` is set but ``__EMBER_HOST__`` isn't yet
    // (early in app boot, before the shim lands). The
    // standard shell plugin handles it.
    const invoke = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = { core: { invoke } };
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    const result = await host.openFile("/abs/file.py");
    expect(result).toBe(true);
    expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
      path: "/abs/file.py",
    });
  });

  it("VSCode: dispatches ember:openFile via postMessage", async () => {
    let posted: unknown = null;
    win.acquireVsCodeApi = () => ({
      postMessage: (msg: unknown) => {
        posted = msg;
      },
    });
    const host = new HostMod.Host();
    expect(host.kind).toBe("vscode");
    const result = await host.openFile("/src/x.ts");
    expect(result).toBe(true);
    expect(posted).toEqual({ type: "ember:openFile", path: "/src/x.ts" });
  });

  it("JetBrains: prefers __EMBER_HOST__.openFile over cefQuery", async () => {
    // Same bridge-first precedence as Tauri. The bridge gets
    // injected by the JCEF plugin; cefQuery is the JS-side
    // fallback for the same RPC.
    const bridgeOpen = vi.fn().mockResolvedValue(undefined);
    const cefQuery = vi.fn();
    win.__EMBER_HOST__ = { openFile: bridgeOpen };
    win.cefQuery = cefQuery as CefQueryFn;
    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    await host.openFile("/src/x.py");
    expect(bridgeOpen).toHaveBeenCalledWith("/src/x.py");
    // cefQuery NOT used when bridge is present.
    expect(cefQuery).not.toHaveBeenCalled();
  });

  it("JetBrains: falls back to cefQuery when no bridge", async () => {
    let request: string | undefined;
    win.cefQuery = ((opts: { request: string }) => {
      request = opts.request;
    }) as CefQueryFn;
    const host = new HostMod.Host();
    expect(host.kind).toBe("jetbrains");
    const result = await host.openFile("/src/y.py");
    expect(result).toBe(true);
    const parsed = JSON.parse(request!) as { type: string; path: string };
    expect(parsed.type).toBe("ember:openFile");
    expect(parsed.path).toBe("/src/y.py");
  });

  it("Web: invokes the registered preview fallback and returns false", async () => {
    // No native bridge — fall back to the in-app preview
    // drawer. Returns ``false`` because the open wasn't
    // delegated to the OS / IDE — load-bearing because the
    // caller's UX may want to know the difference (e.g.
    // close a popover only when the IDE took the open).
    const fallback = vi.fn();
    const host = new HostMod.Host();
    host.setPreviewFallback(fallback);
    expect(host.kind).toBe("web");
    const result = await host.openFile("/src/z.py");
    expect(result).toBe(false);
    expect(fallback).toHaveBeenCalledWith("/src/z.py");
  });

  it("Web without a preview fallback returns false silently", async () => {
    // No fallback registered (rare — App.tsx registers one
    // at boot — but defensive). The web host returns false
    // without raising.
    const host = new HostMod.Host();
    expect(host.kind).toBe("web");
    expect(await host.openFile("/x.py")).toBe(false);
  });

  it("Tauri with a partial shim falls back to the fallback (not crash)", async () => {
    // Defensive — Tauri injected ``__TAURI__`` but neither
    // ``__EMBER_HOST__.openFile`` nor ``core.invoke`` is
    // available. The host should NOT crash; it falls through
    // to the fallback if registered, else returns false.
    win.__TAURI__ = {}; // no core.invoke, no bridge
    const fallback = vi.fn();
    const host = new HostMod.Host();
    host.setPreviewFallback(fallback);
    const result = await host.openFile("/x.py");
    // Fell to fallback → false.
    expect(result).toBe(false);
    expect(fallback).toHaveBeenCalledWith("/x.py");
  });

  it("swallows exceptions from bridge.openFile", async () => {
    // The catch-all try/except in the source means a crashing
    // bridge shouldn't propagate the throw to the FE caller.
    // The console.warn is the only side effect.
    win.__TAURI__ = {};
    win.__EMBER_HOST__ = {
      openFile: () => {
        throw new Error("bridge crashed");
      },
    };
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fallback = vi.fn();
    const host = new HostMod.Host();
    host.setPreviewFallback(fallback);
    const result = await host.openFile("/x.py");
    expect(result).toBe(false);
    // After the exception, the fallback fires.
    expect(fallback).toHaveBeenCalledWith("/x.py");
    consoleWarn.mockRestore();
  });
});

// ── Host.canOpenNatively / setPreviewFallback ──────────────

describe("Host.canOpenNatively + fallback registration", () => {
  it("canOpenNatively is false on plain web", () => {
    const host = new HostMod.Host();
    expect(host.kind).toBe("web");
    expect(host.canOpenNatively).toBe(false);
  });

  it("canOpenNatively is true on Tauri", () => {
    win.__TAURI__ = {};
    const host = new HostMod.Host();
    expect(host.canOpenNatively).toBe(true);
  });

  it("canOpenNatively is true on VSCode", () => {
    win.acquireVsCodeApi = () => ({ postMessage: () => undefined });
    const host = new HostMod.Host();
    expect(host.canOpenNatively).toBe(true);
  });

  it("canOpenNatively is true on JetBrains", () => {
    win.cefQuery = (() => undefined) as CefQueryFn;
    const host = new HostMod.Host();
    expect(host.canOpenNatively).toBe(true);
  });

  it("setPreviewFallback registers the callback for openFile", async () => {
    // Sanity-check the registration API itself (covered
    // implicitly above, but pin the public contract). The
    // fallback receives the path the user clicked.
    const host = new HostMod.Host();
    const fallback = vi.fn();
    host.setPreviewFallback(fallback);
    await host.openFile("/registered.py");
    expect(fallback).toHaveBeenCalledWith("/registered.py");
  });
});
