/**
 * EmberClient — WebSocket protocol client for the Ember Code backend.
 *
 * Responsibilities:
 *  - connect/reconnect to `python -m ember_code.backend --ws-port N`
 *  - correlate request/response pairs via the `id` field
 *  - demultiplex streamed run events (same `id` until `stream_end`)
 *  - fan out uncorrelated events (status updates, push notifications)
 *
 * The WS URL is provided by the embedding shell (Tauri / VSCode /
 * JetBrains) via `?ws=` query param or `window.__EMBER_WS_URL__`;
 * `ws://127.0.0.1:8765` is the dev-server fallback.
 */

import type { RPCResponse, ServerMessage } from "./messages";
import { fe } from "./messages";

declare global {
  interface Window {
    __EMBER_WS_URL__?: string;
  }
}

export function resolveWsUrl(): string {
  const param = new URLSearchParams(window.location.search).get("ws");
  return param || window.__EMBER_WS_URL__ || "ws://127.0.0.1:8765";
}

export type ConnectionState =
  | "connecting"
  | "connected"
  | "disconnected"
  /** Displaced by a newer tab/webview (BE close 1008). No
   *  auto-reconnect — reconnecting would kick the other tab and
   *  start a war. The user reconnects explicitly. */
  | "replaced";

type StreamHandler = (msg: ServerMessage) => void;

let nextId = 0;
function genId(prefix: string): string {
  nextId += 1;
  return `${prefix}-${Date.now().toString(36)}-${nextId}`;
}

const RPC_TIMEOUT_MS = 60_000;

export class EmberClient {
  private ws: WebSocket | null = null;
  private url: string;
  private closed = false;
  /** Per-request stream handlers, keyed by message id. */
  private streams = new Map<string, StreamHandler>();
  /** One-shot RPC resolvers, keyed by message id. */
  private rpcWaiters = new Map<
    string,
    { resolve: (m: ServerMessage) => void; reject: (e: Error) => void; timer: number }
  >();
  /** Uncorrelated event listeners (status updates, pushes, HITL...). */
  private eventListeners = new Set<StreamHandler>();
  private stateListeners = new Set<(s: ConnectionState) => void>();
  private reconnectDelay = 500;

  constructor(url: string = resolveWsUrl()) {
    this.url = url;
  }

  connect(): void {
    // Reset the closed latch: React StrictMode mounts effects twice
    // (mount → cleanup → mount), and cleanup calls close(). Without
    // the reset, the second mount's connect() would no-op and the UI
    // would sit on "Connecting…" forever.
    this.closed = false;

    // Tear down any previous socket BEFORE creating a new one. An
    // orphaned CONNECTING socket would otherwise win the race for
    // the BE's single-client slot and lock every subsequent
    // reconnect out with close code 1008.
    const old = this.ws;
    if (old) {
      old.onopen = old.onmessage = old.onclose = old.onerror = null;
      try {
        old.close();
      } catch {
        /* already closed */
      }
    }

    this.emitState("connecting");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    // Every handler checks it still belongs to the active socket —
    // events from a replaced socket must not flip the shared state.
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.reconnectDelay = 500;
      this.emitState("connected");
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      this.dispatch(JSON.parse(ev.data as string) as ServerMessage);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.failPending(new Error("connection closed"));
      // 1008 "replaced by a newer client": another tab took the BE
      // slot. Yield — do NOT auto-reconnect, or the two tabs would
      // displace each other forever. The UI offers manual reconnect.
      if (ev.code === 1008) {
        this.closed = true;
        this.emitState("replaced");
        return;
      }
      this.emitState("disconnected");
      if (!this.closed) {
        setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 5_000);
      }
    };
    ws.onerror = () => {
      // onclose follows; reconnect handled there.
    };
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
  }

  onEvent(fn: StreamHandler): () => void {
    this.eventListeners.add(fn);
    return () => this.eventListeners.delete(fn);
  }

  onStateChange(fn: (s: ConnectionState) => void): () => void {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  }

  // ── Core flows ────────────────────────────────────────────────────

  /** This view's identity, assigned by the BE's Welcome at attach.
   *  Used to skip rendering echoes of our own messages/typing. */
  clientId = "";

  /**
   * Send a user message; `onMessage` receives every streamed event
   * for this run. Resolves when `stream_end` arrives.
   */
  runMessage(text: string, onMessage: StreamHandler): Promise<void> {
    const id = genId("run");
    return this.stream(fe.userMessage(text, id, this.clientId), id, onMessage);
  }

  /** Queue a message while a run is in flight. Fire-and-forget. */
  queueMessage(text: string): void {
    this.send(fe.queueMessage(text, this.clientId));
  }

  private typingTimer: number | null = null;
  private typingPending: string | null = null;

  /**
   * Broadcast this view's live composer draft (mirroring). Trailing-
   * edge throttled to ~10/s; always flushes the final value so other
   * views never display a stale draft.
   */
  sendTyping(text: string): void {
    this.typingPending = text;
    if (this.typingTimer !== null) return;
    this.typingTimer = window.setTimeout(() => {
      this.typingTimer = null;
      if (this.typingPending === null) return;
      const value = this.typingPending;
      this.typingPending = null;
      try {
        this.send(fe.typing(value, this.clientId));
      } catch {
        /* disconnected — drafts are best-effort */
      }
    }, 100);
  }

  /**
   * Execute a slash command; resolves with the CommandResult.
   * Commands are request/reply (single correlated message, no
   * stream_end) — see the Command branch in backend/__main__.py.
   */
  handleCommand(text: string): Promise<ServerMessage> {
    const id = genId("cmd");
    return this.request(fe.command(text, id), id);
  }

  /** Resolve a batch of HITL requirements; streams the resumed run. */
  resolveHitlBatch(
    decisions: { requirement_id: string; action: "confirm" | "reject"; choice: string }[],
    onMessage: StreamHandler,
  ): Promise<void> {
    const id = genId("hitl");
    return this.stream(
      fe.hitlResponseBatch(
        decisions.map((d) => ({ ...d, choice: d.choice as never })),
        id,
      ),
      id,
      onMessage,
    );
  }

  cancel(): void {
    this.send(fe.cancel());
  }

  /**
   * Generic RPC, correlated by id. The BE replies with either an
   * `rpc_response` envelope (plain values) or a typed protocol
   * message carrying the request id directly (e.g. `get_status` →
   * `status_update`) — see _handle_message in backend/__main__.py.
   * Both shapes resolve here; typed messages resolve as themselves.
   */
  rpc<T = unknown>(method: string, args: Record<string, unknown> = {}): Promise<T> {
    const id = genId("rpc");
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.rpcWaiters.delete(id);
        reject(new Error(`RPC ${method} timed out`));
      }, RPC_TIMEOUT_MS);
      this.rpcWaiters.set(id, {
        resolve: (m) => {
          if (m.type === "rpc_response") {
            const r = m as RPCResponse;
            if (r.error) reject(new Error(r.error));
            else resolve(r.result as T);
          } else {
            resolve(m as unknown as T);
          }
        },
        reject,
        timer,
      });
      this.send(fe.rpcRequest(method, args, id));
    });
  }

  /** Switch model — request/reply, resolves with the BE's Info ack. */
  switchModel(modelName: string): Promise<ServerMessage> {
    const id = genId("model");
    return this.request(fe.modelSwitch(modelName, id), id);
  }

  /** @-mention file completions (BE-side FileIndex). */
  completeFiles(query: string, limit = 50): Promise<string[]> {
    return this.rpc<string[]>("complete_files", { query, limit });
  }

  /** $-prefix shell mode — captured output from the BE. */
  runShell(command: string): Promise<{ output: string; exit_code: number }> {
    return this.rpc("run_shell", { command });
  }

  /**
   * Start the browser login flow. Progress arrives as push
   * notifications on channels `login_status` / `login_result`.
   */
  login(): Promise<{ started: boolean }> {
    return this.rpc("login", {});
  }

  cancelLogin(): void {
    this.send({ type: "cancel_login" });
  }

  mcpToggle(serverName: string, connect: boolean): Promise<ServerMessage> {
    const id = genId("mcp");
    return this.request(
      { type: "mcp_toggle", id, server_name: serverName, connect },
      id,
    );
  }

  // ── Internals ─────────────────────────────────────────────────────

  /** One-shot request/reply correlated by id (reuses the RPC waiter map). */
  private request(payload: object, id: string): Promise<ServerMessage> {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.rpcWaiters.delete(id);
        reject(new Error("request timed out"));
      }, RPC_TIMEOUT_MS);
      this.rpcWaiters.set(id, { resolve, reject, timer });
      try {
        this.send(payload);
      } catch (e) {
        clearTimeout(timer);
        this.rpcWaiters.delete(id);
        reject(e);
      }
    });
  }

  private stream(
    payload: object,
    id: string,
    onMessage: StreamHandler,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      this.streams.set(id, (m) => {
        if (m.type === "stream_end") {
          this.streams.delete(id);
          resolve();
          return;
        }
        onMessage(m);
      });
      try {
        this.send(payload);
      } catch (e) {
        this.streams.delete(id);
        reject(e);
      }
    });
  }

  private send(payload: object): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error("not connected to backend");
    }
    this.ws.send(JSON.stringify(payload));
  }

  private dispatch(msg: ServerMessage): void {
    if (msg.type === "welcome") {
      this.clientId = msg.client_id;
      // Fall through to listeners so the app can react to attach.
    }

    const id = msg.id ?? "";

    // RPC replies are correlated purely by id: either an rpc_response
    // envelope or a typed message echoed back with the request id.
    if (id && this.rpcWaiters.has(id)) {
      const waiter = this.rpcWaiters.get(id)!;
      this.rpcWaiters.delete(id);
      clearTimeout(waiter.timer);
      waiter.resolve(msg);
      return;
    }

    const stream = id ? this.streams.get(id) : undefined;
    if (stream) {
      stream(msg);
      return;
    }

    // Uncorrelated: status pushes, HITL arriving outside a tracked
    // stream, scheduler notifications, etc.
    for (const fn of this.eventListeners) fn(msg);
  }

  private failPending(err: Error): void {
    for (const [, waiter] of this.rpcWaiters) {
      clearTimeout(waiter.timer);
      waiter.reject(err);
    }
    this.rpcWaiters.clear();
    // Streams: synthesize stream_end so awaiting callers resolve and
    // the UI unblocks rather than hanging on a dead socket.
    for (const [sid, fn] of this.streams) {
      fn({ type: "stream_end", id: sid } as ServerMessage);
    }
    this.streams.clear();
  }

  private emitState(s: ConnectionState): void {
    for (const fn of this.stateListeners) fn(s);
  }
}
