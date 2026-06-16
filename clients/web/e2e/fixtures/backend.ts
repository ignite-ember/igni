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
function defaultHandlers(): Record<string, RpcHandler> {
  return {
    get_session_id: () => "test-session-001",
    attach_session: (args) => ({
      session_id: String(args.session_id || "test-session-001"),
      project_dir: "/tmp/test-project",
    }),
    count_context_tokens: () => 0,
    get_status: () => ({
      type: "status_update",
      model: "test-model",
      context_tokens: 0,
      max_context: 200_000,
      cloud_connected: false,
      cloud_org: "",
    }),
    list_sessions: () => ({ sessions: [] }),
    list_chat_history: () => [],
    get_skill_definitions: () => [],
    refresh_cache: () => ({ ok: true }),
    get_codeindex_state: () => ({ state: "unknown" }),
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
      const data = JSON.stringify(envelope);
      for (const ws of clients) {
        if (ws.readyState === WebSocket.OPEN) ws.send(data);
      }
    },
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
