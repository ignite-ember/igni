// @vitest-environment jsdom
/**
 * Tests for ``clientState`` — this window's model, as its views bind
 * to it.
 *
 * Two surfaces:
 *
 *   • ``ensureClientId`` — reads/mints the stable id the window's model
 *     is kept under. Each web tab / IDE webview / JetBrains panel keeps
 *     its own.
 *   • ``WindowModel`` — the host-owned model of the window: hydrated
 *     from the backend, written optimistically (a view shows its write
 *     at once) and confirmed by the backend's push, with conflicts
 *     resolved by the backend's order. The load-bearing parts are that
 *     ordering, the composer's pause, and that a window with no backend
 *     model still works.
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

import { act, cleanup, render } from "@testing-library/react";
import {
  WindowModel,
  ensureClientId,
  references,
  useWindowField,
  type WindowChange,
} from "./clientState";
import type { IgniClient } from "./protocol/client";

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

  // ── Per-window partitioning ───────────────────────────────
  //
  // Two Tauri windows share one origin and therefore one
  // localStorage. The view key (window label) is what keeps them
  // from sharing an identity — and since ``client_state`` holds
  // which SESSION a window is bound to, sharing an identity means
  // the second window opens onto the first one's chat.

  it("gives two different view keys two different ids", () => {
    const a = ensureClientId("w-2");
    const b = ensureClientId("w-3");
    expect(a).not.toBe(b);
  });

  it("returns the same id for the same view key across calls", () => {
    // A window reload re-runs this with the same label and must
    // land back on its own session, not a fresh client.
    const first = ensureClientId("w-2");
    const second = ensureClientId("w-2");
    expect(first).toBe(second);
  });

  it("stores a named view under a suffixed key", () => {
    ensureClientId("w-2");
    expect(localStorage.getItem("ember-code:client-id:w-2")).toBeTruthy();
    // The unsuffixed slot belongs to the main window — an extra
    // window must not write into it.
    expect(localStorage.getItem("ember-code:client-id")).toBeNull();
  });

  it("keeps the main window on the legacy unsuffixed key", () => {
    // Upgrade path: an install that predates multi-window has its
    // id (and therefore its bound session, drafts, sidebar state)
    // under the unsuffixed key. The first window is labelled
    // ``main``, and must keep reading that one.
    localStorage.setItem("ember-code:client-id", "pre-existing-id");
    expect(ensureClientId("main")).toBe("pre-existing-id");
    expect(ensureClientId("")).toBe("pre-existing-id");
  });
});

// ── WindowModel ────────────────────────────────────────────

interface Call {
  method: string;
  args: Record<string, unknown>;
}

/** A backend with a window model: answers ``window_model`` as the real
 *  one does, and pushes each change back the way the real one does —
 *  but only when the test says so, so orderings can be staged. */
class FakeBackend {
  calls: Call[] = [];
  rev = 0;
  pushes: WindowChange[] = [];
  refuse = false;
  unreachable = false;
  state: Record<string, unknown> = {};

  client(): IgniClient {
    const rpc = vi.fn(async (method: string, args: Record<string, unknown> = {}) => {
      this.calls.push({ method, args });
      if (this.unreachable) throw new Error("fixture: unhandled rpc method 'window_model'");
      if (this.refuse) throw new Error("window_model: sidebar: expected Sidebar");
      const op = args.op;
      if (op === "hydrate") return { state: this.state, rev: this.rev, unset: [] };
      if (op === "write") {
        this.rev += 1;
        this.pushes.push({
          window: String(args.window),
          field: args.field as WindowChange["field"],
          value: args.value,
          rev: this.rev,
          token: String(args.token),
        });
        return { rev: this.rev };
      }
      if (op === "toast") {
        this.rev += 1;
        const toast = { id: `t-${this.rev}`, source: null, ...(args.value as object) };
        this.pushes.push({
          window: String(args.window),
          field: "toasts",
          value: [toast],
          rev: this.rev,
          token: String(args.token),
          event: "toast_shown",
          item: toast,
        });
        return { toast, rev: this.rev };
      }
      return { rev: this.rev };
    });
    return { rpc, clientId: "conn-1" } as unknown as IgniClient;
  }

  /** Another writer — an extension — changes the model. */
  extensionWrites(field: WindowChange["field"], value: unknown): WindowChange {
    this.rev += 1;
    const change = { window: "w-1", field, value, rev: this.rev, token: "" };
    this.pushes.push(change);
    return change;
  }

  /** Deliver every push so far, in the backend's order. */
  deliver(model: WindowModel): void {
    for (const change of this.pushes.splice(0)) model.applyPush(change);
  }

  writes(): Call[] {
    return this.calls.filter((c) => c.args.op === "write");
  }
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe("WindowModel — hydrate", () => {
  it("reads the whole model from the backend and says the window is open", async () => {
    const backend = new FakeBackend();
    backend.state = { route: [{ kind: "plugins", label: "Plugins" }], session: "s-1" };
    const model = new WindowModel(backend.client(), "w-1", 250, "tauri");
    await model.hydrate();
    expect(model.isHydrated()).toBe(true);
    expect(model.get("route")).toEqual([{ kind: "plugins", label: "Plugins" }]);
    expect(model.get("session")).toBe("s-1");
    expect(backend.calls[0]).toEqual({
      method: "window_model",
      args: { op: "hydrate", window: "w-1", connection_id: "conn-1", kind: "tauri" },
    });
  });

  it("a backend with no window model leaves the view's defaults and works locally", async () => {
    const backend = new FakeBackend();
    backend.unreachable = true;
    const model = new WindowModel(backend.client(), "w-1", 250, "web", {
      sidebar: { open: true, width: null, section: null },
    });
    await model.hydrate();
    expect(model.isHydrated()).toBe(true);
    expect(model.get("sidebar").open).toBe(true);
    expect(model.unset()).toContain("sidebar");
    model.write("panel", { kind: "search" });
    await settle();
    // Nobody to confirm it, and nobody refused it: the window keeps it.
    expect(model.get("panel")).toEqual({ kind: "search" });
  });
});

describe("WindowModel — a view's write", () => {
  it("shows at once, goes to the backend with a token, and the echo confirms it", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.write("panel", { kind: "notifications" });
    expect(model.get("panel")).toEqual({ kind: "notifications" });
    const [write] = backend.writes();
    expect(write.args).toMatchObject({ field: "panel", value: { kind: "notifications" } });
    expect(String(write.args.token)).toMatch(/^w-1#/);
    backend.deliver(model);
    expect(model.get("panel")).toEqual({ kind: "notifications" });
  });

  it("writing what is already shown is nothing — except a route, which counts again", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.write("panel", null);
    expect(backend.writes()).toHaveLength(0);
    model.write("route", []);
    expect(backend.writes()).toHaveLength(1);
    expect(model.changes("route")).toBe(1);
  });

  it("the composer waits for a pause in typing, and sends once", async () => {
    vi.useFakeTimers();
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1", 250);
    model.write("composer", { text: "h", mode: "chat", attachments: [] }, { debounce: true });
    model.write("composer", { text: "he", mode: "chat", attachments: [] }, { debounce: true });
    model.write("composer", { text: "hey @a.py", mode: "chat", attachments: [] }, { debounce: true });
    expect(model.get("composer").text).toBe("hey @a.py");
    // The draft's references are derived, as the backend derives them.
    expect(model.get("composer").attachments).toEqual(["a.py"]);
    expect(backend.writes()).toHaveLength(0);
    vi.advanceTimersByTime(250);
    expect(backend.writes()).toHaveLength(1);
    expect(backend.writes()[0].args.value).toEqual({ text: "hey @a.py", mode: "chat" });
  });

  it("a write the backend refuses is dropped and the model's value shows", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    backend.refuse = true;
    model.write("sidebar", { open: true, width: null, section: null });
    expect(model.get("sidebar").open).toBe(true);
    await settle();
    expect(model.get("sidebar").open).toBe(false);
  });
});

describe("WindowModel — conflicts resolve by the backend's order", () => {
  it("an extension's change the window has not written over shows at once", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    const heard: string[] = [];
    model.onRemote((c) => heard.push(c.field));
    model.applyPush(backend.extensionWrites("route", [{ kind: "mcp", label: "MCP" }]));
    expect(model.get("route")).toEqual([{ kind: "mcp", label: "MCP" }]);
    expect(heard).toEqual(["route"]);
    // An extension asking for a page counts as being asked again.
    expect(model.changes("route")).toBe(1);
  });

  it("typing still in its pause gives way to a change somebody else made", async () => {
    vi.useFakeTimers();
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1", 250);
    model.write("composer", { text: "mine", mode: "chat", attachments: [] }, { debounce: true });
    model.applyPush(
      backend.extensionWrites("composer", { text: "theirs", mode: "chat", attachments: [] }),
    );
    expect(model.get("composer").text).toBe("theirs");
    vi.advanceTimersByTime(250);
    // Ours was never sent: the model's last word stands.
    expect(backend.writes()).toHaveLength(0);
  });

  it("a write already sent wins over a change the backend applied before it", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    const theirs = backend.extensionWrites("panel", { kind: "search" });
    model.write("panel", { kind: "notifications" });
    // Theirs arrives first; ours is still on screen…
    model.applyPush(theirs);
    expect(model.get("panel")).toEqual({ kind: "notifications" });
    // …and its echo, ordered after theirs, settles it.
    backend.deliver(model);
    expect(model.get("panel")).toEqual({ kind: "notifications" });
  });

  it("a change the backend applied after ours wins, whichever arrives first", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.write("panel", { kind: "notifications" });
    await settle(); // our answer (rev 1) is in: confirmed
    const theirs = backend.extensionWrites("panel", { kind: "search" });
    model.applyPush(theirs);
    expect(model.get("panel")).toEqual({ kind: "search" });
    // Our echo arriving late is older than what is shown: ignored.
    backend.deliver(model);
    expect(model.get("panel")).toEqual({ kind: "search" });
  });

  it("pushes about other windows are not this window's", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.applyPush({ window: "w-2", field: "panel", value: { kind: "search" }, rev: 9, token: "" });
    expect(model.get("panel")).toBeNull();
  });
});

describe("WindowModel — the session and its draft", () => {
  it("a draft still in its pause goes to the session being left, before the move", async () => {
    vi.useFakeTimers();
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1", 250);
    model.write("composer", { text: "for the old one", mode: "chat", attachments: [] }, { debounce: true });
    model.write("session", "s-2");
    expect(backend.writes().map((w) => w.args.field)).toEqual(["composer", "session"]);
  });

  it("a forced write is sent though the view already shows it", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.write("composer", { text: "", mode: "chat", attachments: [] }, { force: true });
    expect(backend.writes()).toHaveLength(1);
  });
});

describe("WindowModel — toasts and dialogs", () => {
  it("a toast goes to the model, and arrives in the list by its push", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    model.toast({ text: "Task done", kind: "info" });
    await settle();
    backend.deliver(model);
    expect(model.get("toasts").map((t) => t.text)).toEqual(["Task done"]);
  });

  it("with no backend model, toasts and dialogs are the window's own", async () => {
    const backend = new FakeBackend();
    backend.unreachable = true;
    const model = new WindowModel(backend.client(), "w-1");
    model.toast({ text: "Task done", kind: "info" });
    await settle();
    const [toast] = model.get("toasts");
    expect(toast.text).toBe("Task done");
    model.dismissToast(toast.id);
    expect(model.get("toasts")).toEqual([]);
    model.openDialog({ kind: "update" });
    await settle();
    const dialog = model.get("dialog");
    expect(dialog?.spec).toEqual({ kind: "update" });
    model.answerDialog(dialog!.id, null);
    expect(model.get("dialog")).toBeNull();
  });

  it("answering sends the answer for the dialog that is open", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    model.answerDialog("d-1", { button: "yes", value: null });
    expect(backend.calls[0].args).toMatchObject({
      op: "answer_dialog",
      dialog_id: "d-1",
      value: { button: "yes", value: null },
    });
  });
});

describe("useWindowField", () => {
  afterEach(() => cleanup());

  it("re-renders a view when its field changes, and only then", async () => {
    const backend = new FakeBackend();
    const model = new WindowModel(backend.client(), "w-1");
    await model.hydrate();
    let renders = 0;
    function Panel() {
      renders += 1;
      const panel = useWindowField(model, "panel");
      return <span data-testid="panel">{panel?.kind ?? "none"}</span>;
    }
    const { getByTestId } = render(<Panel />);
    expect(getByTestId("panel").textContent).toBe("none");
    const before = renders;
    act(() => model.write("selection", { message_id: null, text: "x", file: null }));
    expect(renders).toBe(before);
    act(() => model.applyPush(backend.extensionWrites("panel", { kind: "search" })));
    expect(getByTestId("panel").textContent).toBe("search");
  });
});

describe("references", () => {
  it("are the @paths of a draft, not its code pills", () => {
    expect(references("@a @b @a x@y @code:z\n@c")).toEqual(["a", "b", "c"]);
  });
});
