/**
 * Tests for the host-capability layer: Tauri (``__TAURI__`` and the
 * ``__IGNI_HOST__`` bridge it injects) and the plain browser.
 *
 * Vitest's default env is "node" in this project (no jsdom). We
 * stub the bits of ``window`` that ``host.ts`` actually touches.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

interface WindowLike {
  __TAURI__?: unknown;
  __TAURI_INTERNALS__?: unknown;
  __IGNI_HOST__?: {
    openFile?: (p: string) => unknown;
    notify?: (payload: { title: string; body?: string }) => unknown;
    openUrl?: (url: string) => unknown;
  };
  open?: (url: string, target?: string, features?: string) => unknown;
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

// A minimal event-listener registry with browser ``postMessage``
// semantics.
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
  // Reset modules each test so no module-level state leaks between
  // cases.
  vi.resetModules();
  win = makeWindow();
  (globalThis as { window?: WindowLike }).window = win;
  HostMod = await import("./host");
});

afterEach(() => {
  delete (globalThis as { window?: WindowLike }).window;
});

// ── Host.notify — OS notification dispatch ────────────
//
// The notify() bridge surfaces project-level async events
// (scheduled task finished, etc.) as native OS banners
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

  it("dispatches via __IGNI_HOST__.notify when bridge is wired", async () => {
    // The bridge takes precedence over the per-host branch
    // — host-injected APIs tend to support richer notifications than the generic
    // plugin paths.
    const bridgeNotify = vi.fn().mockResolvedValue(undefined);
    win.__IGNI_HOST__ = { notify: bridgeNotify };
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

  it("swallows exceptions from the dispatch and returns false", async () => {
    // The notify wrapper is wrapped in a try/catch with
    // console.warn — must not propagate. Otherwise a buggy
    // bridge would crash the FE caller.
    const bridgeNotify = vi.fn().mockRejectedValue(new Error("boom"));
    win.__IGNI_HOST__ = { notify: bridgeNotify };
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const host = new HostMod.Host();
    expect(await host.notify({ title: "T" })).toBe(false);
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

describe("Host.openExternal", () => {
  it("accepts only https links with a host and no credentials", () => {
    const ok = HostMod.isOpenableLink;
    expect(ok("https://code.example.com/repos/42?tab=x#y")).toBe(true);
    expect(ok("http://code.example.com/")).toBe(false);
    expect(ok("javascript:alert(1)")).toBe(false);
    expect(ok("file:///etc/passwd")).toBe(false);
    expect(ok("vscode://open")).toBe(false);
    expect(ok("https://user:pw@example.com/")).toBe(false);
    expect(ok("https://example.com/a b")).toBe(false);
    expect(ok("/relative")).toBe(false);
    expect(ok("")).toBe(false);
  });

  it("Tauri: hands the link to the opener bridge", async () => {
    const openUrl = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = {};
    win.__IGNI_HOST__ = { openUrl };
    const host = new HostMod.Host();
    expect(await host.openExternal("https://example.com/x")).toBe(true);
    expect(openUrl).toHaveBeenCalledWith("https://example.com/x");
  });

  it("a browser tab opens a new tab with no way back", async () => {
    const open = vi.fn().mockReturnValue(null);
    win.open = open;
    const host = new HostMod.Host();
    expect(await host.openExternal("https://example.com/x")).toBe(true);
    expect(open).toHaveBeenCalledWith("https://example.com/x", "_blank", "noopener,noreferrer");
  });

  it("refuses a link it may not open, and opens nothing", async () => {
    const openUrl = vi.fn();
    const open = vi.fn();
    win.__TAURI__ = {};
    win.__IGNI_HOST__ = { openUrl };
    win.open = open;
    const host = new HostMod.Host();
    expect(await host.openExternal("http://example.com/")).toBe(false);
    expect(openUrl).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
  });
});

describe("Host.openFile", () => {
  it("returns false for an empty path (defensive)", async () => {
    // The composer occasionally builds a pill with an empty
    // path during drag-cancel races. Don't fire any bridge.
    const host = new HostMod.Host();
    expect(await host.openFile("")).toBe(false);
  });

  it("Tauri: uses __IGNI_HOST__.openFile when wired", async () => {
    // Bridge first — Tauri shells inject a richer
    // ``__IGNI_HOST__.openFile`` that knows about the
    // user's project root.
    const bridgeOpen = vi.fn().mockResolvedValue(undefined);
    win.__TAURI__ = {};
    win.__IGNI_HOST__ = { openFile: bridgeOpen };
    const host = new HostMod.Host();
    expect(host.kind).toBe("tauri");
    const result = await host.openFile("/abs/file.py");
    expect(result).toBe(true);
    expect(bridgeOpen).toHaveBeenCalledWith("/abs/file.py");
  });

  it("Tauri: falls back to plugin:shell|open when no bridge", async () => {
    // ``__TAURI__`` is set but ``__IGNI_HOST__`` isn't yet
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

  it("Web: invokes the registered preview fallback and returns false", async () => {
    // No native bridge — fall back to the in-app preview
    // drawer. Returns ``false`` because the open wasn't
    // delegated to the OS — load-bearing because the
    // caller's UX may want to know the difference (e.g.
    // close a popover only when the OS took the open).
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
    // ``__IGNI_HOST__.openFile`` nor ``core.invoke`` is
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
    win.__IGNI_HOST__ = {
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

// ── Host.viewKey ──────────────────────────────────────────
//
// The window label is what makes two Tauri windows of one app
// instance distinguishable. They share an origin, so they share
// localStorage, so without a per-window key they would share the
// persisted client_id — and therefore the session each is bound to.
// See ``clientState.ts``.

describe("Host.viewKey", () => {
  it("reads the label from the global Tauri API", () => {
    win.__TAURI__ = { window: { getCurrentWindow: () => ({ label: "w-2" }) } };
    const host = new HostMod.Host();
    expect(host.viewKey).toBe("w-2");
  });

  it("falls back to the internals metadata", () => {
    // ``withGlobalTauri`` can be off, or the global API can land
    // later than the bundle; the internals object is injected
    // before any page script either way.
    win.__TAURI_INTERNALS__ = { metadata: { currentWindow: { label: "w-3" } } };
    const host = new HostMod.Host();
    expect(host.viewKey).toBe("w-3");
  });

  it("falls back to the internals metadata when getCurrentWindow throws", () => {
    // Outside a real webview the global API is present but its
    // accessor throws. That must not take out identity resolution.
    win.__TAURI__ = {
      window: {
        getCurrentWindow: () => {
          throw new Error("not in a Tauri webview");
        },
      },
    };
    win.__TAURI_INTERNALS__ = { metadata: { currentWindow: { label: "w-4" } } };
    const host = new HostMod.Host();
    expect(host.viewKey).toBe("w-4");
  });

  it("is empty on plain web", () => {
    // Single-view hosts keep their pre-multi-window behaviour:
    // ``ensureClientId("")`` lands on the legacy storage key.
    const host = new HostMod.Host();
    expect(host.viewKey).toBe("");
  });

  it("caches the label once seen", () => {
    // A window keeps its label for its lifetime, so re-reading a
    // resolved key must not go back to the globals.
    win.__TAURI__ = { window: { getCurrentWindow: () => ({ label: "w-5" }) } };
    const host = new HostMod.Host();
    expect(host.viewKey).toBe("w-5");
    delete win.__TAURI__;
    expect(host.viewKey).toBe("w-5");
  });
});
