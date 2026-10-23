/**
 * What crosses the gap between the host app and an extension's frame.
 *
 * One envelope type, versioned, with the host and the guest reading
 * the same definition. The guest half of this file is the part that
 * becomes a published package when somebody outside this repository
 * writes an extension; until then both sides import it from here, so
 * there is one description of the wire rather than two that agree
 * until they don't.
 *
 * The frame is the untrusted party. Everything the host accepts from
 * it is checked — the origin it came from, the window it came from,
 * the nonce it was issued, and whether the method is one this
 * extension was granted. None of those checks live in the guest.
 */

/** Bumped only for a change the other side cannot ignore. */
export const BRIDGE_VERSION = 1;

/** Host → guest, once, before anything else is accepted. */
export interface HelloMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "hello";
  /** Issued per frame, per session. Every guest message must carry it. */
  nonce: string;
  /** The extension this frame is, so a guest can label its own logs. */
  extensionId: string;
  /** Opaque per-extension launch parameters, from the host. */
  params: Record<string, unknown>;
  /**
   * What `contributes.settings` is configured to, as an
   * administrator left it.
   *
   * Separate from `params` on purpose. `params` is where you are —
   * a deep link, replayed from `location`. This is how you are set
   * up. Merging them would mean a setting called `doc` and a link
   * carrying `?doc=` were the same key, and whichever the host
   * wrote last would win silently.
   *
   * Sent in `hello` and nowhere else: a change applies at the next
   * mount. Reconfiguring a frame mid-render is a harder promise
   * than it is worth until somebody asks for it, and the host's
   * `set_extension_settings` says the same thing.
   *
   * Optional on the wire, not in this host: it was added after
   * the envelope shipped, so a guest must treat its absence as
   * "nothing configured" rather than as a malformed greeting.
   */
  settings?: Record<string, unknown>;
  /** `"light"` or `"dark"`, following the host. */
  theme: string;
  /**
   * The published design tokens, as the host currently computes them.
   *
   * Sent rather than fetched: the guest is a different origin and its
   * own CSP refuses a cross-origin stylesheet. Sent rather than
   * baked into the guest's package, because a copy of a theme defined
   * across forty-five blocks drifts within a release.
   */
  tokens: Record<string, string>;
}

/** Host → guest, whenever the host's theme changes. */
export interface ThemeMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "theme";
  theme: string;
  tokens: Record<string, string>;
}

/**
 * Host → guest: the launch parameters changed under a mounted frame.
 *
 * `params` used to arrive exactly once, inside `hello`, and there
 * was no way to send new ones. That is the difference between a
 * widget and a page: a page can be linked to, and a link that
 * resolves while the frame is already mounted had nowhere to land.
 * The host would have had to tear the frame down and rebuild it,
 * losing whatever state it held, to deliver a changed query string.
 */
export interface ParamsMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "params";
  params: Record<string, unknown>;
}

/**
 * Guest → host: where I am now.
 *
 * **Not a navigation.** `navigate` asks the host to go somewhere
 * else; this says "I am still here, and here is now this". The host
 * folds it into its own URL and replays it in the next `hello`, so a
 * reload or a shared link comes back to the same place inside the
 * extension.
 *
 * It exists because an extension has no other way to be addressable.
 * It cannot write the address bar — `allow-top-navigation` is
 * deliberately absent from the frame's sandbox — and it cannot
 * persist anything, because each extension is served from an
 * ephemeral port, a port is part of an origin, and so `localStorage`
 * is wiped whenever the backend restarts.
 *
 * Strings only, and the host is free to cap or drop them: this ends
 * up in a URL, and an extension that puts its whole state here would
 * be writing an unbounded value into the host's address bar.
 */
export interface LocationMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "location";
  nonce: string;
  state: Record<string, string>;
}

/**
 * Host → guest: something happened.
 *
 * Two kinds travel in one envelope, because the guest handles them
 * identically — a topic and a payload — and two mechanisms for one
 * job is two things to keep in step.
 *
 * **Host events** are things igni knows: the session changed, HEAD
 * moved. Their topics come from a published vocabulary and an
 * extension must be granted them in `permissions.events`.
 *
 * **An extension's own events** come from its own service, relayed
 * to its own frame, under `ext.<id>.`. No grant: an extension is
 * always allowed to talk to itself, and is never permitted to hear
 * another.
 *
 * The grant is checked where the event is dispatched, never here. A
 * host that posted everything and relied on this file to discard
 * what was not ours would be relying on the untrusted party to
 * discard it.
 */
export interface EventMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "event";
  /** Dotted and lowercase. Never a wildcard — that is grant syntax. */
  topic: string;
  /** Shape is defined per topic by the published vocabulary. */
  payload: Record<string, unknown>;
}

/** Guest → host: call a backend RPC. */
export interface CallMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "call";
  nonce: string;
  /** Correlates the reply. Generated by the guest; echoed, never trusted. */
  id: string;
  method: string;
  args: Record<string, unknown>;
}

/** Guest → host: ask the host app to go somewhere. */
export interface NavigateMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "navigate";
  nonce: string;
  /** A page the host knows. The host decides whether it exists. */
  to: string;
}

/** Guest → host: this surface is finished. */
export interface CloseMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "close";
  nonce: string;
}

/** Host → guest: the answer to one `call`. */
export interface ResultMessage {
  igni: typeof BRIDGE_VERSION;
  kind: "result";
  id: string;
  ok: boolean;
  value?: unknown;
  /** Present when `ok` is false. Always a sentence, never an object. */
  error?: string;
}

export type HostMessage =
  HelloMessage | ThemeMessage | ParamsMessage | EventMessage | ResultMessage;
export type GuestMessage =
  CallMessage | NavigateMessage | LocationMessage | CloseMessage;

/**
 * Is this a message from a guest, shaped the way we require?
 *
 * Written as a narrowing guard rather than a schema check because the
 * host has to answer this about `unknown` arriving from a frame, and
 * the answer has to be "no" for everything that is not exactly right.
 * A message that fails here is dropped without a reply: a guest that
 * can tell malformed from unauthorised has learned something.
 */
export function isGuestMessage(data: unknown): data is GuestMessage {
  if (typeof data !== "object" || data === null) return false;
  const m = data as Record<string, unknown>;
  if (m.igni !== BRIDGE_VERSION) return false;
  if (typeof m.nonce !== "string" || m.nonce.length === 0) return false;
  switch (m.kind) {
    case "call":
      return (
        typeof m.id === "string" &&
        m.id.length > 0 &&
        typeof m.method === "string" &&
        m.method.length > 0 &&
        typeof m.args === "object" &&
        m.args !== null &&
        !Array.isArray(m.args)
      );
    case "navigate":
      return typeof m.to === "string" && m.to.length > 0;
    case "location":
      // Every value a string, checked rather than assumed: this goes
      // into the host's own URL, and a guest is the untrusted party.
      return (
        typeof m.state === "object" &&
        m.state !== null &&
        !Array.isArray(m.state) &&
        Object.values(m.state as Record<string, unknown>).every(
          (value) => typeof value === "string",
        )
      );
    case "close":
      return true;
    default:
      return false;
  }
}

/** Is this a message from the host, shaped the way the guest requires? */
export function isHostMessage(data: unknown): data is HostMessage {
  if (typeof data !== "object" || data === null) return false;
  const m = data as Record<string, unknown>;
  if (m.igni !== BRIDGE_VERSION) return false;
  switch (m.kind) {
    case "hello":
      return (
        typeof m.nonce === "string" &&
        typeof m.extensionId === "string" &&
        typeof m.theme === "string" &&
        typeof m.params === "object" &&
        m.params !== null &&
        // `settings` is not required. It was added after the
        // envelope shipped, and a guest built against this version
        // may be framed by a host that predates it — refusing the
        // greeting would turn an additive field into a version
        // wall. Absent reads as "configured with nothing", which
        // is what an older host means.
        (m.settings === undefined ||
          (typeof m.settings === "object" && m.settings !== null)) &&
        typeof m.tokens === "object" &&
        m.tokens !== null
      );
    case "theme":
      return (
        typeof m.theme === "string" &&
        typeof m.tokens === "object" &&
        m.tokens !== null
      );
    case "params":
      return (
        typeof m.params === "object" &&
        m.params !== null &&
        !Array.isArray(m.params)
      );
    case "event":
      return (
        typeof m.topic === "string" &&
        m.topic.length > 0 &&
        typeof m.payload === "object" &&
        m.payload !== null &&
        !Array.isArray(m.payload)
      );
    case "result":
      return typeof m.id === "string" && typeof m.ok === "boolean";
    default:
      return false;
  }
}

/**
 * May this extension call this method?
 *
 * Exact names, or a `*`-suffixed prefix. No regular expressions and no
 * `*` anywhere but the end: a permission list is read by whoever
 * approves an install, and a pattern language it cannot be read in is
 * a permission nobody actually granted.
 */
export function methodAllowed(
  method: string,
  allow: readonly string[],
): boolean {
  return allow.some((pattern) =>
    pattern.endsWith("*")
      ? method.startsWith(pattern.slice(0, -1))
      : method === pattern,
  );
}
