/**
 * Host capability layer — adapts the FE to whatever runtime is
 * shelling it: a regular browser tab (the web client) or the Tauri
 * desktop client. The rest of the app
 * calls `host.openFile(path)` and doesn't need to know which one.
 *
 * Host detection is by feature, not user-agent:
 *
 *   • Tauri          → injects `window.__TAURI__` and (when our
 *                       desktop client wires the bridge) `__EMBER_HOST__.openFile`
 *   • Plain browser  → no native open available — fall back to the
 *                       in-app preview modal (driven by an onPreview
 *                       callback the caller supplies).
 *
 * Adding a new host = teach `detectHost()` to spot it and provide an
 * `openFile` that returns `true` after dispatching. No other code
 * needs to change.
 */

export type HostKind = "tauri" | "web";

interface NativeBridge {
  openFile?: (path: string) => unknown;
  revealInFolder?: (path: string) => unknown;
  /** Send a desktop notification (OS banner). When this returns
   *  truthy, the FE assumes the host took ownership of the surface;
   *  otherwise the FE shows its in-app toast as the fallback. */
  notify?: (payload: NotifyPayload) => unknown;
  /** Tauri-only: macOS Finder-style title bar — folder name plus
   *  an optional org as a middle-dot suffix. */
  setAppTitle?: (folder: string, org: string) => unknown;
  /** Tauri-only: open a link with the operating system. */
  openUrl?: (url: string) => unknown;
}

/** A link igni may open outside itself: absolute `https:` with a host
 *  and no credentials. The backend checks `host.app.open_external`'s
 *  URL first; this is the window's own check before it opens one. */
export function isOpenableLink(url: string): boolean {
  if (!url || /[\s\u0000-\u001f]/.test(url)) return false;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === "https:" &&
    parsed.hostname !== "" &&
    parsed.username === "" &&
    parsed.password === ""
  );
}

export interface NotifyPayload {
  title: string;
  body?: string;
  /** Opaque token forwarded back to the FE when the user clicks the
   *  native notification. The FE attaches the meaning (e.g. open the
   *  Schedule panel and select task X). */
  data?: Record<string, unknown>;
}

declare global {
  interface Window {
    __TAURI__?: unknown;
    __TAURI_INTERNALS__?: unknown;
    __EMBER_HOST__?: NativeBridge;
  }
}

function detectHost(): HostKind {
  if (typeof window === "undefined") return "web";
  if (window.__TAURI__ || window.__TAURI_INTERNALS__) return "tauri";
  return "web";
}

/**
 * Per-window identity, when the host has a concept of one.
 *
 * Tauri labels every webview window, and that label is what makes
 * two windows of ONE app instance distinguishable: they share an
 * origin, so they share `localStorage`, so without this they would
 * also share the persisted `client_id` — and therefore the session
 * each one is bound to, its composer draft, and its sidebar state.
 * See `clientState.ts`.
 *
 * Every other host shells exactly one view per bundle instance, so
 * they return `""` and callers keep their single-view behaviour.
 *
 * Read synchronously from the globals Tauri injects before any page
 * script runs — `getCurrentWindow()` when the global API is exposed
 * (`withGlobalTauri`), else the internals metadata.
 */
function detectViewKey(): string {
  if (typeof window === "undefined") return "";
  const w = window as unknown as {
    __TAURI__?: { window?: { getCurrentWindow?: () => { label?: string } } };
    __TAURI_INTERNALS__?: { metadata?: { currentWindow?: { label?: string } } };
  };
  try {
    const label = w.__TAURI__?.window?.getCurrentWindow?.()?.label;
    if (label) return label;
  } catch {
    /* getCurrentWindow throws outside a real Tauri webview — fall
       through to the internals metadata. */
  }
  return w.__TAURI_INTERNALS__?.metadata?.currentWindow?.label || "";
}

export class Host {
  /** Cached host kind. We re-detect lazily while it is still
   *  ``"web"``, in case a shell's globals land after the FE bundle's
   *  first ``detectHost()`` runs at module init; once a non-web host
   *  is seen we never look back. */
  private _kind: HostKind;
  /** Cached window label — see `viewKey`. */
  private _viewKey = "";
  /** When the host can't natively open files, the FE shows this
   *  preview instead. Set once at app boot. */
  private fallback: ((path: string) => void) | null = null;
  /** Told of every file opened, whichever way it opened. */
  private openListeners = new Set<(path: string) => void>();

  constructor() {
    this._kind = detectHost();
  }

  get kind(): HostKind {
    // Once we've identified a non-web host, stop re-detecting —
    // those bridges only get richer, never disappear. While still
    // "web", a later Tauri shim injection should still flip us
    // over to the right host.
    if (this._kind === "web") {
      const next = detectHost();
      if (next !== "web") this._kind = next;
    }
    return this._kind;
  }

  /** This view's identity within the host, or `""` when the host has
   *  only one view. Today that means the Tauri window label.
   *
   *  Re-detected while empty for the same reason `kind` is: a host
   *  shim can land after module init. Once a label is seen it never
   *  changes — a window keeps its label for its whole lifetime. */
  get viewKey(): string {
    if (!this._viewKey) this._viewKey = detectViewKey();
    return this._viewKey;
  }

  /** Register the in-app preview opener used by the web fallback. */
  setPreviewFallback(fn: (path: string) => void) {
    this.fallback = fn;
  }

  /** Be told whenever a file is opened — in the host's editor or the
   *  preview. The window's model keeps it as the file picked. */
  onOpen(fn: (path: string) => void): () => void {
    this.openListeners.add(fn);
    return () => {
      this.openListeners.delete(fn);
    };
  }

  /** `true` if the host can open files in something other than the
   *  built-in preview modal. Useful for tooltips. */
  get canOpenNatively(): boolean {
    return this.kind !== "web";
  }

  /**
   * Open ``path`` in whatever the host considers the right place.
   * Returns true if the open attempt was dispatched; false if we
   * fell back to the preview (or had nothing to do at all).
   */
  /**
   * Open an https link in the person's browser, as igni opens every
   * link outside it: the desktop app's opener, else a new tab with no
   * way back to this one. Refuses anything {@link isOpenableLink}
   * does not accept. Returns true if the open was dispatched.
   */
  async openExternal(url: string): Promise<boolean> {
    if (!isOpenableLink(url)) return false;
    try {
      const bridge = window.__EMBER_HOST__;
      if (this.kind === "tauri" && bridge?.openUrl) {
        await Promise.resolve(bridge.openUrl(url));
        return true;
      }
      // ``noopener`` makes ``window.open`` answer null whether or not
      // it opened, so there is nothing to read back.
      window.open(url, "_blank", "noopener,noreferrer");
      return true;
    } catch (e) {
      console.warn("host.openExternal failed for", this.kind, e);
      return false;
    }
  }

  /**
   * Surface a notification through the native host (the OS) when
   * a bridge is wired. Returns ``true`` if dispatched; ``false`` for
   * plain web (the caller should fall back to its in-app toast).
   *
   * Wiring per host:
   *   • ``__EMBER_HOST__.notify`` when the shell injected one
   *   • Tauri          → ``plugin:notification|notify`` (mac/Windows banner)
   */
  async notify(payload: NotifyPayload): Promise<boolean> {
    if (!payload?.title && !payload?.body) return false;
    try {
      const bridge = window.__EMBER_HOST__;
      if (bridge?.notify) {
        await Promise.resolve(bridge.notify(payload));
        return true;
      }
      switch (this.kind) {
        case "tauri": {
          const tauri = (window as unknown as { __TAURI__?: { core?: { invoke?: Function } } })
            .__TAURI__;
          if (tauri?.core?.invoke) {
            await tauri.core.invoke("plugin:notification|notify", {
              options: { title: payload.title, body: payload.body || "" },
            });
            return true;
          }
          break;
        }
        case "web":
          break;
      }
    } catch (e) {
      console.warn("host.notify failed for", this.kind, e);
    }
    return false;
  }

  async openFile(path: string): Promise<boolean> {
    if (!path) return false;
    for (const fn of this.openListeners) fn(path);
    try {
      switch (this.kind) {
        case "tauri": {
          const bridge = window.__EMBER_HOST__;
          if (bridge?.openFile) {
            await Promise.resolve(bridge.openFile(path));
            return true;
          }
          // Tauri injects __TAURI__ but our desktop client may not yet
          // have wired __EMBER_HOST__. Try the standard shell plugin.
          const tauri = (window as unknown as { __TAURI__?: { core?: { invoke?: Function } } })
            .__TAURI__;
          if (tauri?.core?.invoke) {
            await tauri.core.invoke("plugin:shell|open", { path });
            return true;
          }
          break;
        }
        case "web":
          break;
      }
    } catch (e) {
      console.warn("host.openFile failed for", this.kind, e);
    }
    // No native handler — fall back to the in-app preview.
    if (this.fallback) {
      this.fallback(path);
      return false;
    }
    return false;
  }
}

export const host = new Host();
