/**
 * The guest half: what an extension's page imports to reach igni.
 *
 * This is the file that becomes a published package. Nothing in it
 * imports from the rest of the app, and nothing in it is trusted by
 * the host — every check is on the other side. What it owes an
 * extension author is an object that behaves like the client they
 * would have had if their panel were part of the app, so that porting
 * one is not a rewrite.
 *
 * The shape it presents is `rpc(method, args, timeoutMs)`, which is
 * the whole surface the first panel ported behind it used. Everything else here —
 * navigate, close, theme, params — is what a panel needed from the
 * app around it and used to get by being inside it.
 */

import { applyThemeTokens, type ThemeTokens } from "./tokens";

import { HostError, type HostLink, type Signal } from "./hostModel";
import { Host } from "./hostModel.generated";
import {
  BRIDGE_VERSION,
  type CallMessage,
  type CloseMessage,
  type HostRequestMessage,
  type LocationMessage,
  type NavigateMessage,
  type ResultMessage,
  type ServiceTokenMessage,
  isHostMessage,
} from "./protocol";
import {
  type IssuedToken,
  type ServiceBridge,
  ServiceError,
  ServiceLink,
} from "./service";

/** How long a call waits before giving up. The host has its own. */
const DEFAULT_TIMEOUT_MS = 30_000;

/** Members whose answer is the person's, so no timeout applies to them. */
const WAITS_ON_THE_PERSON = new Set(["ui.show_dialog"]);

/** What an extension's page is handed. */
export interface Bridge {
  /** The id this frame is, as the host named it. */
  readonly extensionId: string;
  /** Launch parameters, as the host passed them. */
  readonly params: Record<string, unknown>;
  /**
   * What `contributes.settings` is configured to.
   *
   * Fixed for the life of this frame — a change applies at the next
   * mount, so there is no `onSettings`. Separate from {@link params}
   * because that is *where you are* and this is *how you are set
   * up*; one namespace for both would make a setting and a query
   * key with the same name the same thing.
   */
  readonly settings: Record<string, unknown>;
  /** The host's theme now. Use `onTheme` to follow it. */
  readonly theme: string;
  /**
   * The published design tokens, already applied to this document's
   * root. Here as a reading for anything that needs a value rather
   * than a CSS variable — a canvas, a chart library.
   */
  readonly tokens: ThemeTokens;
  /** Call a backend RPC. Rejects with the host's refusal, as a sentence. */
  rpc<T = unknown>(
    method: string,
    args?: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<T>;
  /** Ask the host app to navigate. It decides whether to. */
  navigate(to: string): void;
  /** Tell the host this surface is finished. */
  close(): void;
  /** Follow the host's theme. Returns an unsubscribe. */
  onTheme(fn: (theme: string) => void): () => void;
  /**
   * Follow the launch parameters.
   *
   * They arrive in `hello` and can change afterwards — a link
   * resolved while this frame was already mounted, or the host
   * restoring a saved location. Without this the host would have to
   * rebuild the frame to deliver a changed parameter, losing
   * whatever state it held.
   *
   * Fires only on change, never for the initial value; read
   * {@link params} for that.
   */
  onParams(fn: (params: Record<string, unknown>) => void): () => void;
  /**
   * Tell the host where inside this extension the user now is.
   *
   * Not a navigation — {@link navigate} is that. This says "still
   * here, and here is now this". The host folds it into its own URL
   * and replays it in the next `hello`, which is the only way an
   * extension can be linked to or restored: the frame cannot write
   * the address bar, and its `localStorage` dies with the ephemeral
   * port it was served from.
   *
   * Strings only, and keep it small — it ends up in the host's URL.
   * Call it when the user lands somewhere worth returning to, not on
   * every keystroke.
   */
  setLocation(state: Record<string, string>): void;

  /**
   * igni, as this extension sees it: the host object model
   * (`docs/HOST_OBJECT_MODEL.md` in igni). The same model the
   * extension's process reaches as `ext.host`.
   *
   *   const busy = await bridge.host.chat.processing.get();
   *   await bridge.host.chat.post("Two issues in the files you changed.");
   *
   * A page's `host.ui` is the window it is framed in. Every object needs
   * its grant in the manifest's `permissions.host`; one that is not
   * granted rejects with a `HostError` whose `code` is `not_granted`.
   */
  readonly host: Host;

  /**
   * Handle a host signal: `bridge.on(bridge.host.chat.run_completed, fn)`.
   *
   * The first handler of a signal subscribes to it; removing the last
   * one unsubscribes. A signal whose object is not granted never
   * arrives — and subscribing to it is not an error. Returns an
   * unsubscribe.
   */
  on<T>(signal: Signal<T>, fn: (value: T) => void): () => void;

  /**
   * The extension's own service, reached directly, as the signed-in
   * person:
   *
   *   const res = await bridge.service.fetch("/v1/notes");
   *   const socket = await bridge.service.websocket("/v1/rooms/a");
   *
   * The host fetches a token whose audience is this extension's service
   * and nothing else; it is kept and renewed before it expires. No
   * `permissions.host` grant: having a service is the grant — see
   * `docs/HOST_OBJECT_MODEL.md` in igni. `url` is `null` when the
   * extension has none.
   */
  readonly service: ServiceBridge;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  /** `undefined` for a request with no limit (one the person answers). */
  timer: ReturnType<typeof setTimeout> | undefined;
}

/**
 * Wait for the host's `hello`, then hand back the bridge.
 *
 * Connecting is asynchronous because the host issues the nonce, and
 * without one there is nothing to say. An extension that renders
 * before awaiting this is rendering before it knows which extension
 * it is.
 */
export function connect(options: { timeoutMs?: number } = {}): Promise<Bridge> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const host = window.parent;
  if (host === window) {
    return Promise.reject(
      new Error(
        "igni bridge: this page is not framed by a host, so there is nothing to connect to",
      ),
    );
  }

  return new Promise<Bridge>((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener("message", onHello);
      reject(
        new Error(`igni bridge: no hello from the host within ${timeoutMs}ms`),
      );
    }, timeoutMs);

    function onHello(event: MessageEvent) {
      // The host is the only window that can reach this frame's
      // parent slot, so checking the source is the check that matters.
      // The origin is not pinned here: the guest does not know which
      // origin the app is served from, and learning it from the
      // message would make the check circular.
      if (event.source !== host) return;
      if (!isHostMessage(event.data) || event.data.kind !== "hello") return;
      clearTimeout(timer);
      window.removeEventListener("message", onHello);
      resolve(build(host, event.data.nonce, event.data));
    }

    window.addEventListener("message", onHello);
  });
}

function build(
  host: Window,
  nonce: string,
  hello: {
    extensionId: string;
    params: Record<string, unknown>;
    settings?: Record<string, unknown>;
    serviceUrl?: string | null;
    theme: string;
    tokens: ThemeTokens;
  },
): Bridge {
  const pending = new Map<string, Pending>();
  const themeWatchers = new Set<(theme: string) => void>();
  const paramWatchers = new Set<(params: Record<string, unknown>) => void>();
  let params = hello.params;
  let theme = hello.theme;
  let tokens = hello.tokens;
  let counter = 0;
  const hostPending = new Map<string, Pending>();
  const signalHandlers = new Map<string, Set<(value: unknown) => void>>();
  const tokenPending = new Map<string, Pending>();

  // Applied before the page renders anything, so an extension never
  // paints once in the fallback palette and again in the real one.
  applyThemeTokens(tokens, document.documentElement);

  window.addEventListener("message", (event: MessageEvent) => {
    if (event.source !== host) return;
    if (!isHostMessage(event.data)) return;

    if (event.data.kind === "theme") {
      theme = event.data.theme;
      tokens = event.data.tokens;
      applyThemeTokens(tokens, document.documentElement);
      for (const fn of themeWatchers) fn(theme);
      return;
    }
    if (event.data.kind === "params") {
      params = event.data.params;
      for (const fn of paramWatchers) fn(params);
      return;
    }
    if (event.data.kind === "signal") {
      const handlers = signalHandlers.get(event.data.signal);
      if (handlers === undefined) return;
      for (const fn of handlers) fn(event.data.value);
      return;
    }
    if (event.data.kind === "host-reply") {
      const reply = event.data;
      const waiter = hostPending.get(reply.id);
      if (waiter === undefined) return;
      hostPending.delete(reply.id);
      clearTimeout(waiter.timer);
      if (reply.ok) waiter.resolve(reply.result);
      else
        waiter.reject(
          new HostError(
            reply.error?.code ?? "failed",
            reply.error?.message ?? "the host refused without saying why",
          ),
        );
      return;
    }
    if (event.data.kind === "service-token-reply") {
      const reply = event.data;
      const waiter = tokenPending.get(reply.id);
      if (waiter === undefined) return;
      tokenPending.delete(reply.id);
      clearTimeout(waiter.timer);
      if (reply.ok && typeof reply.token === "string")
        waiter.resolve({ token: reply.token, expiresIn: reply.expiresIn ?? 0 });
      else
        waiter.reject(
          new ServiceError(
            "refused",
            reply.error || "igni gave no token for this extension's service",
          ),
        );
      return;
    }
    if (event.data.kind !== "result") return;

    const reply = event.data as ResultMessage;
    const waiter = pending.get(reply.id);
    // An id we are not waiting on is either a late reply to something
    // that timed out, or noise. Either way there is no one to tell.
    if (waiter === undefined) return;
    pending.delete(reply.id);
    clearTimeout(waiter.timer);
    if (reply.ok) waiter.resolve(reply.value);
    else
      waiter.reject(
        new Error(
          reply.error || "the host refused the call without saying why",
        ),
      );
  });

  function post(
    message:
      | CallMessage
      | NavigateMessage
      | LocationMessage
      | CloseMessage
      | HostRequestMessage
      | ServiceTokenMessage,
  ) {
    // Targeted at "*" deliberately: the guest does not know the host's
    // origin, and guessing one would mean a message silently going
    // nowhere the first time the app is served from somewhere else.
    // Nothing secret travels in this direction — the host authorises,
    // the guest only asks.
    host.postMessage(message, "*");
  }

  function hostRequest(
    body: Omit<HostRequestMessage, "igni" | "kind" | "nonce" | "id">,
  ): Promise<unknown> {
    const id = `h${++counter}`;
    // A dialog waits on the person, for as long as they take.
    const waits = WAITS_ON_THE_PERSON.has(body.path ?? "");
    return new Promise<unknown>((resolve, reject) => {
      const timer = waits
        ? undefined
        : setTimeout(() => {
            hostPending.delete(id);
            reject(
              new HostError(
                "failed",
                `igni bridge: host.${body.path || body.op} timed out after ${DEFAULT_TIMEOUT_MS}ms`,
              ),
            );
          }, DEFAULT_TIMEOUT_MS);
      hostPending.set(id, { resolve, reject, timer });
      post({ igni: BRIDGE_VERSION, kind: "host", nonce, id, ...body });
    });
  }

  const link: HostLink = {
    request(op, path, options) {
      return hostRequest({
        op,
        path,
        window: options.window ?? null,
        ...(options.session ? { session: options.session } : {}),
        ...(options.params !== undefined ? { params: options.params } : {}),
        ...(op === "set" ? { value: options.value } : {}),
      });
    },
  };
  const hostModel = new Host(link);

  /** One token request to the host, which asks igni as this extension. */
  function askForToken(): Promise<IssuedToken> {
    const id = `s${++counter}`;
    return new Promise<IssuedToken>((resolve, reject) => {
      const timer = setTimeout(() => {
        tokenPending.delete(id);
        reject(
          new ServiceError(
            "failed",
            `igni bridge: no service token within ${DEFAULT_TIMEOUT_MS}ms`,
          ),
        );
      }, DEFAULT_TIMEOUT_MS);
      tokenPending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      post({ igni: BRIDGE_VERSION, kind: "service-token", nonce, id });
    });
  }
  const service = new ServiceLink(hello.serviceUrl ?? null, askForToken);

  return {
    extensionId: hello.extensionId,
    host: hostModel,
    service,
    on<T>(signal: Signal<T>, fn: (value: T) => void) {
      const wire = signal.name;
      let handlers = signalHandlers.get(wire);
      if (handlers === undefined) {
        handlers = new Set();
        signalHandlers.set(wire, handlers);
        void hostRequest({ op: "subscribe", path: "", signals: [wire] }).catch(
          () => {
            /* Refused or unknown: nothing will arrive, which is the
               promise either way. */
          },
        );
      }
      const handler = fn as (value: unknown) => void;
      handlers.add(handler);
      return () => {
        const current = signalHandlers.get(wire);
        if (current === undefined) return;
        current.delete(handler);
        if (current.size === 0) {
          signalHandlers.delete(wire);
          void hostRequest({
            op: "unsubscribe",
            path: "",
            signals: [wire],
          }).catch(() => {});
        }
      };
    },
    get params() {
      return params;
    },
    // Absent from an older host's greeting, which means the same
    // thing as empty: nothing has been configured.
    settings: hello.settings ?? {},
    get theme() {
      return theme;
    },
    get tokens() {
      return tokens;
    },
    rpc<T>(
      method: string,
      args: Record<string, unknown> = {},
      timeoutMs: number = DEFAULT_TIMEOUT_MS,
    ): Promise<T> {
      const id = `${++counter}`;
      return new Promise<T>((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(
            new Error(`igni bridge: ${method} timed out after ${timeoutMs}ms`),
          );
        }, timeoutMs);
        pending.set(id, {
          resolve: resolve as (v: unknown) => void,
          reject,
          timer,
        });
        post({ igni: BRIDGE_VERSION, kind: "call", nonce, id, method, args });
      });
    },
    navigate(to: string) {
      post({ igni: BRIDGE_VERSION, kind: "navigate", nonce, to });
    },
    close() {
      post({ igni: BRIDGE_VERSION, kind: "close", nonce });
    },
    onTheme(fn: (next: string) => void) {
      themeWatchers.add(fn);
      return () => themeWatchers.delete(fn);
    },
    onParams(fn: (next: Record<string, unknown>) => void) {
      paramWatchers.add(fn);
      return () => paramWatchers.delete(fn);
    },
    setLocation(state: Record<string, string>) {
      post({ igni: BRIDGE_VERSION, kind: "location", nonce, state });
    },
  };
}
