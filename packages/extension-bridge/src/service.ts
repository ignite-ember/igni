/**
 * `bridge.service`: the extension's own service, from its page, as the
 * signed-in person.
 *
 *   const notes = await bridge.service.fetch("/v1/notes");
 *   const room = await bridge.service.websocket("/v1/rooms/runbook");
 *
 * The page talks to its service directly — the host is for host things.
 * What the host does is the one thing a page cannot: trade the person's
 * igni session for a ten-minute token whose audience is **this
 * extension's service and nothing else**. The page asks for "my token";
 * igni decides whose service that is, from which extension the frame
 * is. The same exchange the extension's process uses (`ext.service`),
 * and the same cache.
 *
 * The token never leaves for anywhere but the service: `fetch` and
 * `websocket` refuse a path that resolves to another origin, so a page
 * cannot hand it to an egress host by accident. The frame's CSP admits
 * the service's origin (`https`/`wss`) and the hosts the manifest
 * declared in `permissions.egress`, and nothing else.
 */

/** Why `bridge.service` could not do what was asked. */
export class ServiceError extends Error {
  constructor(
    /**
     * `no_service` (none a page may reach), `refused` (igni gave no
     * token — nobody signed in, say), `other_origin`, `closed`, `failed`.
     */
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ServiceError";
  }
}

/** What the host hands back for one token request. */
export interface IssuedToken {
  token: string;
  /** Seconds it has left. */
  expiresIn: number;
}

/** How a socket says who it is, once it is open. */
export interface ServiceSocketOptions {
  /**
   * The first frame, carrying the token. A browser's WebSocket cannot
   * set an `Authorization` header, and a token in the URL ends up in
   * access logs, so the token goes in the first frame. The default is
   * `{"type": "auth", "token": …}` — what igni's services read (the
   * Artifacts document room, for one).
   */
  authFrame?: (token: string) => string;
  /** Sub-protocols, passed to the WebSocket as they are. */
  protocols?: string | string[];
}

/** `bridge.service`, as a page uses it. */
export interface ServiceBridge {
  /**
   * The service's URL, or `null` when this extension has none a page
   * may reach — then every other member rejects with `no_service`.
   */
  readonly url: string | null;
  /**
   * A bearer token for the service. Kept until shortly before it
   * expires and fetched again then; concurrent asks share one fetch.
   */
  token(): Promise<string>;
  /**
   * `fetch` against the service, with the token. `path` is relative
   * to {@link url}. A `401` fetches a new token and is sent once more.
   */
  fetch(path: string, init?: RequestInit): Promise<Response>;
  /**
   * A WebSocket to `path` on the service (`https` → `wss`), resolved
   * once it is open and has said who it is (see
   * {@link ServiceSocketOptions.authFrame}).
   */
  websocket(path: string, options?: ServiceSocketOptions): Promise<WebSocket>;
}

/** Fetch a new token when the held one has less than this left. */
export const RENEW_BEFORE_MS = 60_000;

/** The default first frame: `{"type": "auth", "token": …}`. */
export function authMessage(token: string): string {
  return JSON.stringify({ type: "auth", token });
}

/** `bridge.service`'s implementation; the bridge builds one per frame. */
export class ServiceLink implements ServiceBridge {
  private held: { token: string; expiresAt: number } | null = null;
  private fetching: Promise<string> | null = null;

  constructor(
    readonly url: string | null,
    private readonly ask: () => Promise<IssuedToken>,
    private readonly env: {
      now?: () => number;
      fetch?: typeof fetch;
      WebSocket?: typeof WebSocket;
    } = {},
  ) {}

  token(): Promise<string> {
    return this.fresh(false);
  }

  async fetch(path: string, init: RequestInit = {}): Promise<Response> {
    const target = this.resolve(path, false);
    const send = async (token: string) => {
      const headers = new Headers(init.headers);
      headers.set("Authorization", `Bearer ${token}`);
      const doFetch = this.env.fetch ?? globalThis.fetch.bind(globalThis);
      return doFetch(target, { ...init, headers });
    };
    const first = await send(await this.fresh(false));
    if (first.status !== 401) return first;
    // Revoked or expired early: one new token, one more try.
    return send(await this.fresh(true));
  }

  async websocket(
    path: string,
    options: ServiceSocketOptions = {},
  ): Promise<WebSocket> {
    const target = this.resolve(path, true);
    const token = await this.fresh(false);
    const Socket = this.env.WebSocket ?? globalThis.WebSocket;
    const frame = (options.authFrame ?? authMessage)(token);
    return new Promise<WebSocket>((resolve, reject) => {
      const socket = new Socket(target, options.protocols);
      const opened = () => {
        socket.removeEventListener("open", opened);
        socket.removeEventListener("close", failed);
        socket.send(frame);
        resolve(socket);
      };
      const failed = () => {
        socket.removeEventListener("open", opened);
        socket.removeEventListener("close", failed);
        reject(
          new ServiceError("closed", `the service closed ${path} before it opened`),
        );
      };
      socket.addEventListener("open", opened);
      // `error` is always followed by `close`; one rejection is enough.
      socket.addEventListener("close", failed);
    });
  }

  private async fresh(force: boolean): Promise<string> {
    if (this.url === null) {
      throw new ServiceError(
        "no_service",
        "this extension has no service its page may reach",
      );
    }
    const now = (this.env.now ?? Date.now)();
    if (!force && this.held && this.held.expiresAt - now > RENEW_BEFORE_MS) {
      return this.held.token;
    }
    if (this.fetching) return this.fetching;
    this.fetching = this.ask()
      .then((issued) => {
        this.held = {
          token: issued.token,
          expiresAt: (this.env.now ?? Date.now)() + issued.expiresIn * 1000,
        };
        return issued.token;
      })
      .finally(() => {
        this.fetching = null;
      });
    return this.fetching;
  }

  /** `path` on the service, or a refusal if it is anywhere else. */
  private resolve(path: string, socket: boolean): string {
    if (this.url === null) {
      throw new ServiceError(
        "no_service",
        "this extension has no service its page may reach",
      );
    }
    const base = new URL(this.url);
    const joined = new URL(
      `${base.pathname.replace(/\/$/, "")}/${path.replace(/^\//, "")}`,
      base,
    );
    // Something like `//elsewhere.example/x` or a full URL would land
    // on another origin, and the token goes only to the service.
    if (
      /^[a-z][a-z0-9+.-]*:/i.test(path) ||
      path.startsWith("//") ||
      joined.origin !== base.origin
    ) {
      throw new ServiceError(
        "other_origin",
        `'${path}' is not on this extension's service; its token goes nowhere else`,
      );
    }
    if (socket) {
      joined.protocol = joined.protocol === "https:" ? "wss:" : "ws:";
    }
    return joined.toString();
  }
}
