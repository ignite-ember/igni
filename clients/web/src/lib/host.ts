/**
 * Host capability layer — adapts the FE to whatever runtime is
 * shelling it: a regular browser tab, the Tauri desktop client, the
 * VSCode webview, or the JetBrains JCEF panel. The rest of the app
 * calls `host.openFile(path)` and doesn't need to know which one.
 *
 * Host detection is by feature, not user-agent:
 *
 *   • Tauri          → injects `window.__TAURI__` and (when our
 *                       desktop client wires the bridge) `__EMBER_HOST__.openFile`
 *   • VSCode webview → `acquireVsCodeApi()` is present; we postMessage
 *                       up to the extension which then calls
 *                       `vscode.commands.executeCommand("vscode.open", uri)`
 *   • JetBrains JCEF → either `__EMBER_HOST__.openFile` (preferred) or
 *                       `window.cefQuery` from the JCEF query bridge
 *   • Plain browser  → no native open available — fall back to the
 *                       in-app preview modal (driven by an onPreview
 *                       callback the caller supplies).
 *
 * Adding a new host = teach `detectHost()` to spot it and provide an
 * `openFile` that returns `true` after dispatching. No other code
 * needs to change.
 */

export type HostKind = "tauri" | "vscode" | "jetbrains" | "web";

interface NativeBridge {
  openFile?: (path: string) => unknown;
  revealInFolder?: (path: string) => unknown;
  /** Send a desktop / IDE notification (OS banner). When this returns
   *  truthy, the FE assumes the host took ownership of the surface;
   *  otherwise the FE shows its in-app toast as the fallback. */
  notify?: (payload: NotifyPayload) => unknown;
  /** Tauri-only: macOS Finder-style title bar — folder name plus
   *  an optional org as a middle-dot suffix. Other hosts can wire
   *  this to set their window title too; today JetBrains uses the
   *  tool-window header for the same info. */
  setAppTitle?: (folder: string, org: string) => unknown;
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
    acquireVsCodeApi?: () => { postMessage: (m: unknown) => void };
    cefQuery?: (opts: {
      request: string;
      onSuccess?: (r: string) => void;
      onFailure?: (code: number, msg: string) => void;
    }) => void;
  }
}

function detectHost(): HostKind {
  if (typeof window === "undefined") return "web";
  if (window.__TAURI__ || window.__TAURI_INTERNALS__) return "tauri";
  if (typeof window.acquireVsCodeApi === "function") return "vscode";
  if (typeof window.cefQuery === "function") return "jetbrains";
  // Explicit native bridge wins regardless of where it was injected
  // (e.g. JetBrains injects __EMBER_HOST__ but no cefQuery in some
  // setups). Treat it as the JetBrains case so we use postMessage-style.
  if (window.__EMBER_HOST__?.openFile) return "jetbrains";
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

let cachedVsCodeApi: ReturnType<NonNullable<Window["acquireVsCodeApi"]>> | null = null;
function getVsCodeApi() {
  if (cachedVsCodeApi) return cachedVsCodeApi;
  if (typeof window.acquireVsCodeApi !== "function") return null;
  cachedVsCodeApi = window.acquireVsCodeApi();
  return cachedVsCodeApi;
}

export class Host {
  /** Cached host kind. We re-detect lazily on every access because
   *  JCEF (JetBrains) injects ``window.cefQuery`` via ``onLoadEnd``
   *  — AFTER the FE bundle's first ``detectHost()`` runs at module
   *  init. Re-detecting means the first time anything asks
   *  ``host.kind`` after the shim has landed, we upgrade ``"web"``
   *  to ``"jetbrains"`` and never look back. */
  private _kind: HostKind;
  /** Cached window label — see `viewKey`. */
  private _viewKey = "";
  /** When the host can't natively open files, the FE shows this
   *  preview instead. Set once at app boot. */
  private fallback: ((path: string) => void) | null = null;

  constructor() {
    this._kind = detectHost();
  }

  get kind(): HostKind {
    // Once we've identified a non-web host, stop re-detecting —
    // those bridges only get richer, never disappear. While still
    // "web", a later JCEF/Tauri shim injection should still flip us
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
   * Surface a notification through the native host (OS / IDE) when
   * a bridge is wired. Returns ``true`` if dispatched; ``false`` for
   * plain web (the caller should fall back to its in-app toast).
   *
   * Wiring per host:
   *   • Tauri          → ``plugin:notification|notify`` (mac/Windows banner)
   *   • VSCode         → postMessage ``ember:notify`` → extension calls
   *                       ``vscode.window.showInformationMessage``
   *   • JetBrains      → ``__EMBER_HOST__.notify`` (IDE-host injects)
   *                       or ``cefQuery`` JSON request as a fallback
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
        case "vscode": {
          const api = getVsCodeApi();
          if (api) {
            api.postMessage({ type: "ember:notify", ...payload });
            return true;
          }
          break;
        }
        case "jetbrains": {
          if (typeof window.cefQuery === "function") {
            window.cefQuery({
              request: JSON.stringify({ type: "ember:notify", ...payload }),
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

  /**
   * Search project files for an exact-substring match of ``snippet``,
   * routed through the IDE's native indexed search when one is
   * available. JetBrains uses PyCharm's trigram index via JCEF;
   * VSCode uses ``workspace.findFiles`` + content scan. Both are
   * dramatically faster than the BE's ``rg``-subprocess RPC because
   * they skip the subprocess spawn and the cold filesystem walk.
   *
   * Returns ``null`` when the host has no native search (web /
   * Tauri) — the caller should fall back to the WS ``search_code``
   * RPC. The non-null shape mirrors the WS response so the consumer
   * doesn't branch.
   */
  async searchCode(
    snippet: string,
  ): Promise<{
    matches: { path: string; line: number; end_line?: number; preview: string }[];
    truncated: boolean;
  } | null> {
    if (!snippet || snippet.length < 5) return null;

    if (this.kind === "jetbrains") {
      if (typeof window.cefQuery !== "function") return null;
      return new Promise((resolve) => {
        let done = false;
        const finish = (v: unknown) => {
          if (done) return;
          done = true;
          resolve(v as never);
        };
        const t = window.setTimeout(() => finish(null), 3000);
        try {
          window.cefQuery!({
            request: JSON.stringify({ type: "ember:searchCode", snippet }),
            onSuccess: (json) => {
              window.clearTimeout(t);
              try {
                finish(JSON.parse(json));
              } catch {
                finish(null);
              }
            },
            onFailure: () => {
              window.clearTimeout(t);
              finish(null);
            },
          });
        } catch {
          window.clearTimeout(t);
          finish(null);
        }
      });
    }

    if (this.kind === "vscode") {
      // VSCode uses request/response over postMessage. The extension
      // listens for ``ember:searchCode`` and replies with
      // ``ember:searchCodeResult`` carrying the original ``id`` so
      // concurrent searches don't get crossed. The id is just a
      // monotonic counter on this Host instance.
      const api = getVsCodeApi();
      if (!api) return null;
      const id = ++this.vscodeReqCounter;
      return new Promise((resolve) => {
        let done = false;
        const finish = (v: unknown) => {
          if (done) return;
          done = true;
          window.removeEventListener("message", listener);
          resolve(v as never);
        };
        const listener = (ev: MessageEvent) => {
          const data = ev.data as { type?: string; id?: unknown; result?: unknown };
          if (data?.type !== "ember:searchCodeResult") return;
          if (data.id !== id) return;
          finish(data.result ?? null);
        };
        window.addEventListener("message", listener);
        const t = window.setTimeout(() => finish(null), 3000);
        // Cancel the timeout via the listener cleanup.
        window.addEventListener(
          "message",
          () => window.clearTimeout(t),
          { once: true },
        );
        api.postMessage({ type: "ember:searchCode", id, snippet });
      });
    }

    return null;
  }

  private vscodeReqCounter = 0;

  /**
   * Tell the host that a file was just written by the backend so it
   * can refresh its view of disk. JetBrains: VFS refresh →
   * Local History snapshot + editor tab reload. VSCode: revert
   * open document. Other hosts: no-op.
   *
   * Fire-and-forget — there's no UX outcome the caller waits on; if
   * the bridge isn't there, the IDE's own file watcher eventually
   * picks up the change.
   */
  async notifyFileEdited(path: string): Promise<void> {
    if (!path) return;
    try {
      switch (this.kind) {
        case "jetbrains": {
          if (typeof window.cefQuery === "function") {
            window.cefQuery({
              request: JSON.stringify({ type: "ember:fileEdited", path }),
            });
          }
          break;
        }
        case "vscode": {
          const api = getVsCodeApi();
          api?.postMessage({ type: "ember:fileEdited", path });
          break;
        }
        case "tauri":
        case "web":
          break;
      }
    } catch (e) {
      console.warn("host.notifyFileEdited failed", e);
    }
  }

  async openFile(path: string): Promise<boolean> {
    if (!path) return false;
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
        case "vscode": {
          const api = getVsCodeApi();
          if (api) {
            api.postMessage({ type: "ember:openFile", path });
            return true;
          }
          break;
        }
        case "jetbrains": {
          const bridge = window.__EMBER_HOST__;
          if (bridge?.openFile) {
            await Promise.resolve(bridge.openFile(path));
            return true;
          }
          if (typeof window.cefQuery === "function") {
            window.cefQuery({
              request: JSON.stringify({ type: "ember:openFile", path }),
            });
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
