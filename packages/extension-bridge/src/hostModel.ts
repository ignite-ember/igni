/**
 * The pieces `bridge.host` is built from: objects, properties, signals,
 * calls. `hostModel.generated.ts` is generated from igni's host schema
 * and is made of these.
 *
 *   await bridge.host.chat.processing.get();      // read a property
 *   await bridge.host.sessions.current.set(id);   // write one
 *   await bridge.host.chat.post("Done.");         // call a method
 *   bridge.on(bridge.host.chat.run_completed, (run) => { … });
 *
 * How a request reaches igni is the `HostLink`'s business — the bridge
 * forwards it to the window this page is framed in, which forwards it to
 * the host. Names are the schema's, snake_case, in both SDKs.
 */

/** Any JSON value: the host's report of something not yet typed. */
export type JsonValue = unknown;

/** Why the host refused or failed — the same codes the Python SDK raises. */
export type HostErrorCode =
  | "not_granted"
  | "not_found"
  | "invalid"
  | "read_only"
  | "stage_2"
  | "refused"
  | "unavailable"
  | "failed";

/** The host said no, or it went wrong there. `code` is for code; the message, for people. */
export class HostError extends Error {
  constructor(
    readonly code: HostErrorCode | string,
    message: string,
  ) {
    super(message);
    this.name = "HostError";
  }
}

/** How requests reach the igni host. */
export interface HostLink {
  request(
    op: "get" | "set" | "call",
    path: string,
    options: {
      window?: string | null;
      session?: string | null;
      params?: Record<string, unknown>;
      value?: unknown;
    },
  ): Promise<unknown>;
}

/** One object of the host model: `host.chat`, `host.windows.ui(id)`, `host.sessions.chat(id)`. */
export class HostObject {
  constructor(
    protected readonly link: HostLink,
    readonly path: string,
    protected readonly window: string | null = null,
    protected readonly session: string | null = null,
  ) {}

  /** @internal */
  memberPath(name: string): string {
    return this.path ? `${this.path}.${name}` : name;
  }

  /** @internal — for `Property`. */
  hostLink(): HostLink {
    return this.link;
  }

  /** @internal — for `Property`. */
  windowId(): string | null {
    return this.window;
  }

  /** @internal — for `Property`. */
  sessionId(): string | null {
    return this.session;
  }

  protected async call<T>(
    name: string,
    params: Record<string, unknown>,
  ): Promise<T> {
    const sent: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) sent[key] = value;
    }
    return (await this.link.request("call", this.memberPath(name), {
      window: this.window,
      session: this.session,
      params: sent,
    })) as T;
  }
}

/** A value of a host object: `get()` reads it, `set()` writes it. */
export class Property<T> {
  constructor(
    private readonly owner: HostObject,
    readonly name: string,
    readonly writable: boolean,
  ) {}

  get path(): string {
    return this.owner.memberPath(this.name);
  }

  async get(): Promise<T> {
    return (await this.owner.hostLink().request("get", this.path, {
      window: this.owner.windowId(),
      session: this.owner.sessionId(),
    })) as T;
  }

  /** Write it, as the person's UI would. Refused where theirs would be. */
  async set(value: T): Promise<void> {
    if (!this.writable) {
      throw new HostError("read_only", `host.${this.path} cannot be set`);
    }
    await this.owner.hostLink().request("set", this.path, {
      window: this.owner.windowId(),
      session: this.owner.sessionId(),
      value,
    });
  }
}

/**
 * One signal of one host object; `T` is what it carries. `name` is its
 * wire name — `chat.run_completed`, or for the four events that predate
 * the model their topic strings (`session.switched`, …).
 */
export class Signal<T> {
  /** Only for the type parameter; never set. */
  declare readonly carries?: T;

  constructor(readonly name: string) {}
}
