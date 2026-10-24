/**
 * This window's model — the M its views bind to. The host owns it.
 *
 * `docs/HOST_OBJECT_MODEL.md`, *The UI is part of the model*: which page
 * this window is on, the panel over its chat, its sidebar, the preview,
 * what is selected, the composer, toasts, the dialog, where the
 * transcript is scrolled, whether the window has focus, and which
 * session it shows are fields of a model the backend keeps per window
 * (`ember_code.extensions.host.windows`). igni's own views are the
 * first client of that model: they read it through {@link useWindowField}
 * and change it through {@link WindowModel.write} — the same path an
 * extension's `host.ui` takes on the backend, so every change is a
 * signal whoever made it, and an extension's change arrives here as a
 * push the views apply exactly as their own.
 *
 * The model is not cached state beside the views; it is the views'
 * only state. `tests` hold that structurally: `viewState.test.ts`
 * forbids `useState` for anything this model describes.
 *
 * ## Responsive, and still the model's
 *
 * A write shows at once: it is held as *pending* and every view
 * re-renders from it. It is sent as the `window_model` RPC — composer
 * typing after the short pause it always had, everything else
 * immediately — with a token of this window's. The backend applies
 * writes in the order they reach it and pushes each change, with the
 * writer's token, to every window (`window_model_changed`). Then:
 *
 * - **Our own echo** clears the pending write; the pushed value is now
 *   the confirmed one.
 * - **Somebody else's change** (an extension, another window) becomes
 *   the confirmed value. A pending write *not yet sent* — typing inside
 *   the pause — is dropped in its favour: what the model was told last
 *   wins. A pending write already sent stays on screen until its echo,
 *   which the backend necessarily ordered after the change that just
 *   arrived, so it then wins.
 *
 * Either way every window ends on the value the backend applied last.
 * A write the backend *refuses* (it did not fit the field's type) is
 * dropped and the model's value shows. A write that cannot reach the
 * backend at all — no connection, or a backend with no window model,
 * like the e2e fixtures' — is kept: the window goes on working, its
 * own model for the moment.
 */

import { useCallback, useSyncExternalStore } from "react";
import type { IgniClient } from "./protocol/client";
import type { PageRoute } from "./chat/pageStack";

const CLIENT_ID_KEY = "ember-code:client-id";

/**
 * Which `localStorage` slot holds this view's id.
 *
 * `localStorage` is per-origin, so every Tauri window of one app
 * instance shares it. An unpartitioned key would hand all of them
 * the same `client_id` — and since the model holds *which session
 * this window is bound to*, a second window would open onto the
 * first one's chat and fight it over drafts and sidebar state.
 * Partitioning by the host's view key (the Tauri window label) gives
 * each window its own identity while keeping the model on the BE.
 *
 * The first window is labelled `main` and predates multi-window, so
 * it keeps the unsuffixed key. Hosts with a single view (browser,
 * VSCode, JetBrains) pass `""` and land on the same key.
 *
 * Note for whoever wires up "New Window": labels must be stable
 * across app restarts, not minted fresh each launch. A random label
 * per launch means a window can never restore its model, and the
 * BE's store accrues a dead row set per window per run.
 */
function storageKeyFor(viewKey: string): string {
  if (!viewKey || viewKey === "main") return CLIENT_ID_KEY;
  return `${CLIENT_ID_KEY}:${viewKey}`;
}

/** localStorage adapter; swap for the host's API in IDE clients. */
function readClientId(storageKey: string): string {
  try {
    return localStorage.getItem(storageKey) || "";
  } catch {
    return "";
  }
}

function writeClientId(storageKey: string, id: string): void {
  try {
    localStorage.setItem(storageKey, id);
  } catch {
    /* localStorage may be disabled — accept that the window's model
       won't be found again after a reload in that environment. */
  }
}

function newClientId(): string {
  const cr = (globalThis as { crypto?: Crypto }).crypto;
  if (cr && typeof cr.randomUUID === "function") return cr.randomUUID();
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * This view's stable `client_id` — the id its model is kept under —
 * minting one on first use.
 *
 * `viewKey` comes from `host.viewKey` — the Tauri window label, or
 * `""` on hosts that shell a single view.
 */
export function ensureClientId(viewKey = ""): string {
  const storageKey = storageKeyFor(viewKey);
  let id = readClientId(storageKey);
  if (!id) {
    id = newClientId();
    writeClientId(storageKey, id);
  }
  return id;
}

// ── The model's shape (the host schema's `ui` types) ─────────────

/** `Panel`: what is open over the chat. One at a time. */
export interface Panel {
  kind: "notifications" | "search" | "login" | "dir-picker" | string;
  params?: Record<string, unknown> | null;
}

export interface Sidebar {
  open: boolean;
  /** Pixels; `null` for the stylesheet's own width. */
  width: number | null;
  /** The part in view: `links` or `sessions`. */
  section: "links" | "sessions" | null;
}

export interface Preview {
  path: string;
}

export interface Selection {
  /** The picked-out message, by its run id. */
  message_id: string | null;
  /** Text selected in the transcript. */
  text: string | null;
  /** The file last picked. */
  file: string | null;
}

export type ComposerMode = "chat" | "command" | "shell";

export interface Composer {
  text: string;
  /** The `@path` references in `text`. Derived; never written. */
  attachments: string[];
  mode: ComposerMode;
}

export interface ModelToast {
  id: string;
  text: string;
  kind: "info" | "success" | "warning" | "error" | string;
  body?: string | null;
  target?: PageRoute | null;
  /** The extension that showed it; `null` for igni. */
  source?: string | null;
}

export interface Dialog {
  id: string;
  /** `{kind: "update", …}` for igni's own; `{kind: "extension", title,
   *  body, buttons, input}` for an extension's `show_dialog`. */
  spec: Record<string, unknown>;
  source?: string | null;
}

export interface Scroll {
  /** The message at the top of the view, by its run id. */
  message_id: string | null;
  at_bottom: boolean;
}

export interface WindowState {
  session: string;
  project_dir: string;
  route: PageRoute[];
  panel: Panel | null;
  sidebar: Sidebar;
  preview: Preview | null;
  selection: Selection;
  composer: Composer;
  toasts: ModelToast[];
  dialog: Dialog | null;
  scroll: Scroll;
  focused: boolean;
}

export type WindowField = keyof WindowState;

/** The fields a view writes with {@link WindowModel.write}. Toasts and
 *  the dialog change through their own operations, so a stale list
 *  can never drop a toast somebody else just added. */
export type WritableField = Exclude<WindowField, "toasts" | "dialog">;

/** One change, as the backend pushes it (`window_model_changed`). */
export interface WindowChange {
  window: string;
  field: WindowField;
  value: unknown;
  rev: number;
  token: string;
  event?: string;
  item?: Record<string, unknown> | null;
  answer?: unknown;
}

/** What a window is before it has heard from its model. */
export function emptyWindowState(): WindowState {
  return {
    session: "",
    project_dir: "",
    route: [],
    panel: null,
    sidebar: { open: false, width: null, section: null },
    preview: null,
    selection: { message_id: null, text: null, file: null },
    composer: { text: "", attachments: [], mode: "chat" },
    toasts: [],
    dialog: null,
    scroll: { message_id: null, at_bottom: true },
    focused: false,
  };
}

const REFERENCE = /(?:^|(?<=\s))@(?!code:)(\S+)/g;

/** The `@path` references in a draft, in order, without repeats — the
 *  same rule as the backend's `references`. */
export function references(text: string): string[] {
  const seen: string[] = [];
  for (const m of text.matchAll(REFERENCE)) {
    if (!seen.includes(m[1])) seen.push(m[1]);
  }
  return seen;
}

function same(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

/** The backend said no to the write itself, rather than not answering. */
function isRefusal(error: unknown): boolean {
  return error instanceof Error && error.message.startsWith("window_model:");
}

interface Pending {
  value: unknown;
  token: string;
  sent: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  send?: () => void;
}

type RemoteListener = (change: WindowChange) => void;

export interface WriteOptions {
  /** Hold the network write for the store's pause, collapsing a burst
   *  (typing) into one. Only the composer asks for this. */
  debounce?: boolean;
  /** Tell the model even when the view already shows this value — for
   *  a value the model is about to lose, such as a draft carried onto a
   *  new session. */
  force?: boolean;
}

export class WindowModel {
  private confirmed: WindowState;
  /** The model's revision each confirmed field was last set at. A push
   *  or an answer older than that is about a value already replaced. */
  private revs = new Map<WindowField, number>();
  private pending = new Map<WindowField, Pending>();
  private view: WindowState;
  private hydrated = false;
  /** Fields the backend has nothing lasting for — the view's defaults
   *  apply (`unset` in the hydrate answer). */
  private unsetFields: WindowField[] = [];
  private listeners = new Set<() => void>();
  private remoteListeners = new Set<RemoteListener>();
  /** How many times each field changed here, from either side. */
  private counts = new Map<WindowField, number>();
  private seq = 0;
  private localToast = 0;

  constructor(
    private client: IgniClient,
    public readonly clientId: string,
    private debounceMs = 250,
    /** `tauri`, `vscode`, `jetbrains` or `web` — `host.kind`. */
    private viewKind = "web",
    /** What the views show before the model answers. */
    initial: Partial<WindowState> = {},
  ) {
    this.confirmed = { ...emptyWindowState(), ...initial };
    this.view = this.confirmed;
  }

  // ── Reading ───────────────────────────────────────────────────

  /** The whole model as the views should show it: confirmed, with
   *  this window's pending writes over it. A new object on every
   *  change and the same one otherwise. */
  state(): WindowState {
    return this.view;
  }

  get<K extends WindowField>(field: K): WindowState[K] {
    return this.view[field];
  }

  /** How many times `field` changed — a counter a view can key on to
   *  hear "again", such as a link to the page already open. */
  changes(field: WindowField): number {
    return this.counts.get(field) ?? 0;
  }

  isHydrated(): boolean {
    return this.hydrated;
  }

  /** Lasting fields with nothing stored: the view's own defaults apply. */
  unset(): readonly WindowField[] {
    return this.unsetFields;
  }

  /** Be told whenever what {@link state} returns changes. */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Be told of each change somebody else made — an extension, another
   *  window — after it is applied. For what a change *asks* of a view
   *  beyond rendering it: scroll there, switch to that session. */
  onRemote(fn: RemoteListener): () => void {
    this.remoteListeners.add(fn);
    return () => this.remoteListeners.delete(fn);
  }

  // ── The backend ───────────────────────────────────────────────

  /** Read the whole model, once the connection is up. A window that
   *  cannot (no backend model) keeps the empty one and works locally. */
  async hydrate(): Promise<void> {
    try {
      const answer = await this.client.rpc<{
        state: Partial<WindowState>;
        rev: number;
        unset?: WindowField[];
      }>("window_model", {
        op: "hydrate",
        window: this.clientId,
        // Which connection this window is on, and what kind of window
        // it is: `host.windows` is built from this, and learns the
        // window closed when that connection does.
        connection_id: this.client.clientId,
        kind: this.viewKind,
      });
      this.confirmed = { ...this.confirmed, ...(answer?.state ?? {}) };
      this.unsetFields = answer?.unset ?? [];
      for (const field of Object.keys(this.confirmed) as WindowField[]) {
        this.revs.set(field, answer?.rev ?? 0);
      }
    } catch {
      this.unsetFields = ["sidebar"];
    } finally {
      this.hydrated = true;
      this.recompute();
    }
  }

  /** Apply a `window_model_changed` push. Pushes about other windows
   *  are ignored. */
  applyPush(change: WindowChange): void {
    if (change.window !== this.clientId) return;
    const own = Boolean(change.token) && change.token.startsWith(`${this.clientId}#`);
    const fresh = change.rev > (this.revs.get(change.field) ?? -1);
    if (fresh) {
      this.confirmed = { ...this.confirmed, [change.field]: change.value };
      this.revs.set(change.field, change.rev);
    }
    const pending = this.pending.get(change.field);
    if (pending) {
      if (pending.token === change.token) {
        this.pending.delete(change.field);
      } else if (!own && !pending.sent) {
        // Somebody else told the model something while we were still
        // pausing before telling it ours: theirs is the newer word.
        if (pending.timer) clearTimeout(pending.timer);
        this.pending.delete(change.field);
      }
    }
    // Our own echo is not news: the write already counted.
    if (!own && fresh) this.bump(change.field);
    this.recompute();
    if (!own && fresh) for (const fn of this.remoteListeners) fn(change);
  }

  // ── Writing ───────────────────────────────────────────────────

  /** Change one field. Shown at once; told to the model now (or after
   *  the pause, with `debounce`). A write of what is already shown is
   *  nothing. */
  write<K extends WritableField>(
    field: K,
    value: WindowState[K],
    options: WriteOptions = {},
  ): void {
    let next: unknown = value;
    if (field === "composer") {
      const c = value as Composer;
      next = { text: c.text, mode: c.mode, attachments: references(c.text) };
    }
    if (same(this.view[field], next) && field !== "route" && !options.force) return;
    if (field === "session") {
      // The draft is the session's. One still in its pause belongs to
      // the session being left: tell the model now, before the move.
      this.sendNow("composer");
    }
    const previous = this.pending.get(field);
    if (previous?.timer) clearTimeout(previous.timer);
    const token = `${this.clientId}#${++this.seq}`;
    const entry: Pending = { value: next, token, sent: false, timer: null };
    this.pending.set(field, entry);
    this.bump(field);
    this.recompute();
    const send = () => {
      entry.timer = null;
      entry.sent = true;
      const sent = field === "composer" ? { text: (next as Composer).text, mode: (next as Composer).mode } : next;
      void this.client
        .rpc<{ rev: number }>("window_model", {
          op: "write",
          window: this.clientId,
          field,
          value: sent,
          token,
        })
        .then((answer) => this.confirmedAt(field, entry, answer?.rev ?? 0))
        .catch((error) => this.unconfirmed(field, entry, error));
    };
    entry.send = send;
    if (options.debounce) entry.timer = setTimeout(send, this.debounceMs);
    else send();
  }

  private sendNow(field: WindowField): void {
    const entry = this.pending.get(field);
    if (!entry || entry.sent || !entry.send) return;
    if (entry.timer) clearTimeout(entry.timer);
    entry.send();
  }

  /** Show a toast in this window. */
  toast(toast: Omit<ModelToast, "id" | "source">): void {
    void this.client
      .rpc("window_model", {
        op: "toast",
        window: this.clientId,
        value: toast,
        token: `${this.clientId}#${++this.seq}`,
      })
      .catch((error) => {
        if (isRefusal(error)) return;
        // No model to tell: the window shows it on its own account.
        const shown: ModelToast = { id: `local-${++this.localToast}`, source: null, ...toast };
        this.local("toasts", [...this.confirmed.toasts, shown]);
      });
  }

  dismissToast(id: string): void {
    const remaining = this.confirmed.toasts.filter((t) => t.id !== id);
    if (id.startsWith("local-")) {
      this.local("toasts", remaining);
      return;
    }
    void this.client
      .rpc("window_model", {
        op: "dismiss_toast",
        window: this.clientId,
        value: id,
        token: `${this.clientId}#${++this.seq}`,
      })
      .catch((error) => {
        if (!isRefusal(error)) this.local("toasts", remaining);
      });
  }

  /** Open one of igni's own dialogs (`spec.kind`, e.g. `update`). */
  openDialog(spec: Record<string, unknown>): void {
    void this.client
      .rpc("window_model", {
        op: "open_dialog",
        window: this.clientId,
        value: spec,
        token: `${this.clientId}#${++this.seq}`,
      })
      .catch((error) => {
        if (!isRefusal(error)) this.local("dialog", { id: `local-${++this.localToast}`, spec, source: null });
      });
  }

  /** The person answered the open dialog — `null` for dismissed. The
   *  extension that asked gets the answer. */
  answerDialog(id: string, answer: unknown): void {
    if (id.startsWith("local-")) {
      this.local("dialog", null);
      return;
    }
    void this.client
      .rpc("window_model", {
        op: "answer_dialog",
        window: this.clientId,
        dialog_id: id,
        value: answer,
        token: `${this.clientId}#${++this.seq}`,
      })
      .catch((error) => {
        if (!isRefusal(error)) this.local("dialog", null);
      });
  }

  /** Send any write still in its pause — before the page unloads. */
  flush(): void {
    for (const field of [...this.pending.keys()]) this.sendNow(field);
  }

  // ── Internals ─────────────────────────────────────────────────

  /**
   * The model took the write, at revision `rev`. Its answer and its push
   * can arrive in either order; whichever comes first settles the value,
   * and the revision keeps a later change somebody else made from being
   * overwritten by an answer that arrived after it.
   */
  private confirmedAt(field: WindowField, entry: Pending, rev: number): void {
    if (this.pending.get(field) !== entry) return;
    this.pending.delete(field);
    if (rev > (this.revs.get(field) ?? -1)) {
      this.confirmed = { ...this.confirmed, [field]: entry.value };
      this.revs.set(field, rev);
    }
    this.recompute();
  }

  private unconfirmed(field: WindowField, entry: Pending, error: unknown): void {
    if (this.pending.get(field) !== entry) return;
    this.pending.delete(field);
    if (!isRefusal(error)) {
      // Not refused — unreachable. Keep what the person did.
      this.confirmed = { ...this.confirmed, [field]: entry.value };
    }
    this.bump(field);
    this.recompute();
  }

  private local<K extends WindowField>(field: K, value: WindowState[K]): void {
    this.confirmed = { ...this.confirmed, [field]: value };
    this.bump(field);
    this.recompute();
  }

  private bump(field: WindowField): void {
    this.counts.set(field, (this.counts.get(field) ?? 0) + 1);
  }

  private recompute(): void {
    const next: WindowState = { ...this.confirmed };
    for (const [field, entry] of this.pending) {
      (next as unknown as Record<string, unknown>)[field] = entry.value;
    }
    this.view = next;
    for (const fn of this.listeners) fn();
  }
}

/**
 * One field of the window's model, bound: the component re-renders
 * when that field changes and only then.
 */
export function useWindowField<K extends WindowField>(
  model: WindowModel,
  field: K,
): WindowState[K] {
  const subscribe = useCallback((fn: () => void) => model.subscribe(fn), [model]);
  const read = useCallback(() => model.get(field), [model, field]);
  return useSyncExternalStore(subscribe, read, read);
}

