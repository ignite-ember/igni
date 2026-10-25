/**
 * Fixture WebSocket "backend" used by the app-flow Playwright suite.
 *
 * Why: the real BE is Python and depends on a managed venv + a
 * sentence-transformer model + agno + chroma + …; spinning it up
 * per Playwright test is slow, fragile, and tests the BE's
 * behaviour rather than the FE's protocol handling. The fixture
 * implements just enough of the wire protocol that the web client
 * (``protocol/client.ts``) treats it as a real BE:
 *
 *   - Sends ``welcome`` on connect with a stable ``client_id``.
 *   - Answers RPC requests with scripted handlers (per-method
 *     registry); sane defaults cover boot-time RPCs so tests don't
 *     have to script every single ``get_session_id`` /
 *     ``get_status`` / etc.
 *   - Records every inbound message so tests can assert on what the
 *     FE actually put on the wire.
 *   - Exposes ``pushEvent`` for streamed traffic (content_delta,
 *     stream_end, push_notification, …).
 *
 * Each test gets its own fixture instance with a fresh state; the
 * Playwright wrapper (``embed.ts``) handles setup + teardown.
 */

import { WebSocketServer, WebSocket } from "ws";
import type { AddressInfo } from "node:net";

/** RPC handler signature. Args is the raw object the FE sent.
 *  Return the ``result`` value the FE will see in the rpc_response. */
export type RpcHandler = (args: Record<string, unknown>) => unknown;

/** Any JSON object — kept loose so tests can construct arbitrary
 *  protocol shapes without fighting types. */
export type Envelope = Record<string, unknown>;

export interface FixtureBackend {
  /** ws:// URL with a chosen port for the running server. Pass this
   *  to the FE via the ``?ws=`` query param. */
  url: string;
  /** Override or add an RPC handler. Wins over the defaults. */
  onRpc(method: string, handler: RpcHandler): void;
  /** Push an arbitrary envelope to every connected client. */
  pushEvent(envelope: Envelope): void;
  /** All FE→BE messages seen so far, in arrival order. */
  received(): Envelope[];
  /** Tear down the server and disconnect any clients. */
  close(): Promise<void>;
  /** Connected client count — handy for asserting the FE connected
   *  (or detecting flakes where it didn't). */
  clientCount(): number;
  /**
   * The window models this backend keeps, as the real one does
   * (``ember_code.extensions.host.windows``): every ``window_model``
   * request is answered from them and each change is pushed back as
   * ``window_model_changed`` with its revision and the writer's token.
   */
  windows: FixtureWindows;
}

/** One change to a window's model, as the backend pushes it. */
export interface WindowChange {
  window: string;
  field: string;
  value: unknown;
  rev: number;
  token: string;
  event?: string;
  item?: unknown;
  answer?: unknown;
}

/** The fixture's window models — just enough of the real store to
 *  answer a window and to stand in for an extension writing to one. */
export interface FixtureWindows {
  /** The model of one window, field by field. */
  state(window: string): Record<string, unknown>;
  /** The ids of the windows that hydrated. */
  ids(): string[];
  /** Every change so far, in order. */
  changes(): WindowChange[];
  /** An extension writing a field (``host.ui``): stored and pushed with
   *  no window's token, as the real host model does. */
  extensionWrites(window: string, field: string, value: unknown): void;
  /** An extension showing a toast (``host.ui.toast``). */
  extensionToasts(window: string, toast: Record<string, unknown>): string;
  /** An extension asking a question (``host.ui.show_dialog``). */
  extensionAsks(window: string, spec: Record<string, unknown>): string;
}

export interface StartOptions {
  /** Override RPC handlers from the start (more ergonomic than
   *  calling ``onRpc`` after instantiation when the handler must
   *  fire before the first call). */
  rpcHandlers?: Record<string, RpcHandler>;
  /** Stable ``client_id`` returned in the Welcome. Tests sometimes
   *  read it back via ``client.clientId`` and want determinism. */
  clientId?: string;
}

/**
 * Default handlers for the RPCs the web client fires at boot
 * (see ``App.tsx`` around line 257-475 and the typing-effects).
 * Tests can override any of these via ``onRpc``.
 */
/**
 * A status the app will actually render behind.
 *
 * ``App.tsx`` reads ``signedIn`` straight off ``cloud_email``, and an
 * empty one puts ``SignInWall`` in front of the whole app — artifacts
 * are a cloud service and there is no standalone mode, so that is
 * correct behaviour. A test that builds its own status literal and
 * leaves the field out therefore replaces the surface it is testing
 * with a sign-in form, which is how twelve of these tests ended up
 * waiting twenty seconds for a composer that was never going to be
 * there. Spread this instead of writing the shape out.
 */
export function signedInStatus(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    type: "status_update",
    model: "test-model",
    context_tokens: 0,
    max_context: 200_000,
    cloud_connected: true,
    cloud_org: "Fixture Org",
    cloud_email: "fixture@example.com",
    ...overrides,
  };
}

function defaultHandlers(): Record<string, RpcHandler> {
  return {
    get_session_id: () => "test-session-001",
    attach_session: (args) => ({
      session_id: String(args.session_id || "test-session-001"),
      project_dir: "/tmp/test-project",
    }),
    count_context_tokens: () => 0,
    get_status: () => signedInStatus(),
    list_sessions: () => ({ sessions: [] }),
    list_chat_history: () => [],
    get_skill_definitions: () => [],
    refresh_cache: () => ({ ok: true }),
    get_login_state: () => ({ logged_in: false }),
  };
}

export async function startFixtureBackend(
  opts: StartOptions = {},
): Promise<FixtureBackend> {
  const handlers: Record<string, RpcHandler> = {
    ...defaultHandlers(),
    ...(opts.rpcHandlers || {}),
  };
  const clientId = opts.clientId ?? "test-client-001";

  const wss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise<void>((resolve) => wss.once("listening", () => resolve()));
  const port = (wss.address() as AddressInfo).port;
  const url = `ws://127.0.0.1:${port}`;

  const inbound: Envelope[] = [];
  const clients = new Set<WebSocket>();
  const broadcast = (envelope: Envelope) => {
    const data = JSON.stringify(envelope);
    for (const ws of clients) {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    }
  };
  const windows = fixtureWindows(broadcast);
  if (!handlers.window_model) handlers.window_model = (args) => windows.answer(args);

  wss.on("connection", (ws) => {
    clients.add(ws);
    // BE sends Welcome immediately on attach — see
    // ``backend/__main__.py``'s session-pool handshake. The FE
    // expects this to know its own client_id.
    ws.send(JSON.stringify({ type: "welcome", client_id: clientId }));

    ws.on("message", (raw) => {
      let msg: Envelope;
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return; // ignore non-JSON
      }
      inbound.push(msg);

      // RPC requests get matched to handlers; everything else is
      // recorded but otherwise ignored (tests assert on inbound
      // directly, or script push events via ``pushEvent``).
      if (msg.type === "rpc_request") {
        const method = String(msg.method || "");
        const id = String(msg.id || "");
        const handler = handlers[method];
        if (handler) {
          try {
            const result = handler((msg.args as Record<string, unknown>) || {});
            ws.send(JSON.stringify({ type: "rpc_response", id, result }));
          } catch (exc) {
            ws.send(
              JSON.stringify({ type: "rpc_response", id, error: String(exc) }),
            );
          }
        } else {
          // Unhandled RPC — surface as error so the FE doesn't hang
          // waiting for a response. Tests that don't care about a
          // particular method get a fast failure instead of a 60 s
          // timeout.
          ws.send(
            JSON.stringify({
              type: "rpc_response",
              id,
              error: `fixture: unhandled rpc method '${method}'`,
            }),
          );
        }
      }
    });

    ws.on("close", () => {
      clients.delete(ws);
    });
  });

  return {
    url,
    onRpc(method, handler) {
      handlers[method] = handler;
    },
    pushEvent(envelope) {
      broadcast(envelope);
    },
    windows,
    received() {
      return [...inbound];
    },
    clientCount() {
      return clients.size;
    },
    async close() {
      for (const ws of clients) {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      }
      // ``close()`` is invoked by both the test body (for "server
      // crash" scenarios) AND Playwright's teardown — make it
      // idempotent so the second call doesn't reject with "The
      // server is not running".
      await new Promise<void>((resolve) => {
        wss.close(() => resolve());
      });
    },
  };
}

/** A window's model before it has written anything — the real
 *  backend's defaults. */
function emptyWindow(): Record<string, unknown> {
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

function fixtureWindows(
  broadcast: (envelope: Envelope) => void,
): FixtureWindows & { answer(args: Record<string, unknown>): unknown } {
  const models = new Map<string, Record<string, unknown>>();
  const written = new Map<string, Set<string>>();
  const log: WindowChange[] = [];
  let rev = 0;
  let ids = 0;
  const model = (window: string) => {
    let found = models.get(window);
    if (!found) {
      found = emptyWindow();
      models.set(window, found);
      written.set(window, new Set());
    }
    return found;
  };
  const change = (window: string, field: string, value: unknown, token = "", extra: Partial<WindowChange> = {}) => {
    model(window)[field] = value;
    rev += 1;
    const pushed: WindowChange = { window, field, value, rev, token, ...extra };
    log.push(pushed);
    broadcast({
      type: "push_notification",
      channel: "window_model_changed",
      payload: pushed,
    });
    return pushed;
  };
  const references = (text: string) => [
    ...new Set([...text.matchAll(/(?:^|(?<=\s))@(?!code:)(\S+)/g)].map((m) => m[1])),
  ];
  return {
    answer(args) {
      const window = String(args.window ?? "");
      const token = String(args.token ?? "");
      const state = model(window);
      switch (args.op) {
        case "hydrate":
          return {
            state,
            rev,
            unset: written.get(window)!.has("sidebar") ? [] : ["sidebar"],
          };
        case "write": {
          const field = String(args.field);
          let value = args.value;
          if (field === "composer") {
            const c = value as { text: string; mode?: string };
            value = { text: c.text, mode: c.mode ?? "chat", attachments: references(c.text) };
          }
          written.get(window)!.add(field);
          return { rev: change(window, field, value, token).rev };
        }
        case "toast": {
          const toast = { id: `t-${++ids}`, kind: "info", source: null, ...(args.value as object) };
          const list = [...(state.toasts as unknown[]), toast];
          change(window, "toasts", list, token, { event: "toast_shown", item: toast });
          return { toast, rev };
        }
        case "dismiss_toast": {
          const gone = (state.toasts as { id: string }[]).find((t) => t.id === args.value);
          const list = (state.toasts as { id: string }[]).filter((t) => t.id !== args.value);
          change(window, "toasts", list, token, { event: "toast_dismissed", item: gone });
          return { rev };
        }
        case "open_dialog": {
          const dialog = { id: `d-${++ids}`, spec: args.value, source: null };
          change(window, "dialog", dialog, token, { event: "dialog_opened", item: dialog });
          return { dialog, rev };
        }
        case "answer_dialog": {
          const open = state.dialog as { id: string } | null;
          if (!open || open.id !== args.dialog_id) return { answered: false, rev };
          change(window, "dialog", null, token, {
            event: "dialog_closed",
            item: open,
            answer: args.value,
          });
          return { answered: true, rev };
        }
      }
      throw new Error(`window_model: no op ${String(args.op)}`);
    },
    state: (window) => ({ ...model(window) }),
    ids: () => [...models.keys()],
    changes: () => [...log],
    extensionWrites(window, field, value) {
      change(window, field, value);
    },
    extensionToasts(window, toast) {
      const shown = { id: `t-${++ids}`, kind: "info", source: "acme.reviewer", ...toast };
      const list = [...(model(window).toasts as unknown[]), shown];
      change(window, "toasts", list, "", { event: "toast_shown", item: shown });
      return shown.id;
    },
    extensionAsks(window, spec) {
      const dialog = { id: `d-${++ids}`, spec: { kind: "extension", ...spec }, source: "acme.reviewer" };
      change(window, "dialog", dialog, "", { event: "dialog_opened", item: dialog });
      return dialog.id;
    },
  };
}
