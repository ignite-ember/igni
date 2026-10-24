/**
 * The host half: an extension's page, embedded and supervised.
 *
 * Everything the frame says is checked here, because here is the only
 * side that can be. Four checks, and each one is load-bearing:
 *
 *   1. **The window.** `event.source` must be this iframe's own
 *      `contentWindow`. Another frame on the page can post to us too.
 *   2. **The origin.** It must be the origin we loaded. This is the
 *      check that needs the frame to have a real origin at all — from
 *      a sandbox without `allow-same-origin` every extension says
 *      `"null"` and they become indistinguishable. Measured, not
 *      assumed; see `docs/EXTENSIBILITY.md`.
 *   3. **The nonce.** Issued per mount. A frame that navigated
 *      somewhere else and kept posting does not have the new one.
 *   4. **The method.** Against the list this extension was granted.
 *
 * The method check is not the last word, and for a while it was the
 * only word. The frame is untrusted, so the backend re-checks the
 * same grant against the installed manifest — see
 * `ember_code.extensions.registry.ExtensionRegistry.authorise`. This
 * one exists so a refusal is immediate and legible rather than a
 * round trip; it is a courtesy, not the decision.
 *
 * For that re-check to be possible the backend has to know *which*
 * extension is asking, which is what `IgniClient.asExtension` is
 * for: wire `onCall` through it and every call carries the id. Wired
 * to a bare `client.rpc` instead, an `ext.*` call is refused with
 * "this request did not say which extension it came from" — loudly,
 * rather than by being trusted.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  BRIDGE_VERSION,
  type GuestMessage,
  type HostRequestMessage,
  isGuestMessage,
  methodAllowed,
} from "@igni/extension-bridge/protocol";
import { readThemeTokens } from "@igni/extension-bridge/tokens";

/**
 * Are these the same parameters?
 *
 * Shallow and by value, because the common caller writes
 * `params={{ id }}` — a fresh object on every parent render, and
 * identity would make every one of those look like a change and
 * post a message the frame then reacts to.
 */
function sameParams(
  a: Record<string, unknown>,
  b: Record<string, unknown>,
): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every(
    (key) =>
      Object.prototype.hasOwnProperty.call(b, key) && Object.is(a[key], b[key]),
  );
}

/** What the frame is permitted to reach, and where it lives. */
export interface ExtensionFrameProps {
  /** The manifest id. Namespaces everything and labels every log. */
  extensionId: string;
  /** The origin this extension is served from — scheme, host, port. */
  origin: string;
  /** Path within that origin. Defaults to the bundle's index. */
  path?: string;
  /** RPC methods this extension was granted. Exact, or a `foo_*` prefix. */
  allow: readonly string[];
  /**
   * Makes the backend call. The host's client, never handed to the
   * frame.
   *
   * Build it from `client.asExtension(extensionId).rpc` so the call
   * carries its caller; see the note above.
   */
  onCall: (method: string, args: Record<string, unknown>) => Promise<unknown>;
  /** The frame asked the app to go somewhere. The app decides. */
  onNavigate?: (to: string) => void;
  /** The frame says it is finished. */
  onClose?: () => void;
  /**
   * The frame says where inside itself the user now is.
   *
   * Not a navigation — fold it into the host's URL and hand it back
   * as `params` on the next mount. Ignoring it is safe and means the
   * extension is simply not addressable.
   *
   * Values are strings and the host should cap what it keeps: this
   * ends up in an address bar, and the frame is the untrusted party.
   */
  onLocation?: (state: Record<string, string>) => void;
  /**
   * Events for *this* extension, from the host.
   *
   * Already addressed: the backend decided who was entitled to
   * hear each one and sent it to this extension by name. Nothing
   * is filtered here, because filtering here would mean the host
   * had posted somebody else's event into this frame and trusted
   * the frame not to read it.
   */
  /**
   * Events waiting for this frame, oldest first, each with a
   * monotonic `n`.
   *
   * A queue rather than "the latest one", because two pushes that
   * land in the same React batch collapse into one render — so a
   * single slot silently drops whichever arrived first. With three
   * producers that is no longer theoretical: a commit moves HEAD
   * and changes the dirty set at the same moment.
   */
  events?: { topic: string; payload: Record<string, unknown>; n: number }[];
  /**
   * Answers a host-model request (`bridge.host…`) from the frame.
   *
   * The caller forwards it to the backend as this extension — the
   * `host_model` RPC with the frame's extension as `caller` — and the
   * backend checks `permissions.host`. Resolves with the model's reply,
   * refusals included. Absent, every request is refused here.
   */
  onHost?: (
    request: Omit<HostRequestMessage, "igni" | "kind" | "nonce">,
  ) => Promise<{
    ok: boolean;
    result?: unknown;
    error?: { code: string; message: string };
  }>;
  /**
   * Host-model signals for this extension, oldest first, each with a
   * monotonic `n` — queued for the same reason `events` is.
   */
  signals?: { signal: string; value: unknown; n: number }[];
  /** Launch parameters, passed through on `hello`. */
  params?: Record<string, unknown>;
  /**
   * What `contributes.settings` is configured to, passed through on
   * `hello` and never afterwards — a change applies at the next
   * mount, which is what `set_extension_settings` promises.
   *
   * Its own prop rather than folded into `params`: those are a deep
   * link and these are configuration, and one namespace would make
   * a setting named `doc` and a link carrying `?doc=` collide.
   */
  settings?: Record<string, unknown>;
  /** Followed by the frame. */
  theme?: string;
  /** Accessible name for the region. */
  title?: string;
}

/**
 * Everything the frame is allowed to do to the page it sits in.
 *
 * `allow-same-origin` is present, and is not the hole it looks like:
 * the familiar warning is about a frame that is same-origin *with its
 * embedder*, which this never is — the extension has its own port,
 * therefore its own origin, so it cannot reach this document, its
 * storage or its cookies. Without it the frame cannot load its own
 * scripts at all.
 *
 * Deliberately absent: `allow-popups`, `allow-modals`,
 * `allow-top-navigation` and `allow-downloads`. A surface in a panel
 * does not need to move the window it is in.
 */
const SANDBOX = "allow-scripts allow-forms allow-same-origin";

function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function ExtensionFrame({
  extensionId,
  origin,
  path = "/index.html",
  allow,
  onCall,
  onNavigate,
  onClose,
  onLocation,
  onHost,
  events,
  signals,
  params,
  settings,
  theme = "dark",
  title,
}: ExtensionFrameProps) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // One per mount. Regenerating on every render would invalidate the
  // nonce the frame is already using.
  const nonce = useMemo(() => newNonce(), []);
  const src = useMemo(() => origin.replace(/\/$/, "") + path, [origin, path]);

  // Read through refs so the message listener does not have to be torn
  // down and rebuilt whenever a caller passes a new closure — a
  // re-registered listener drops messages in flight.
  const handlers = useRef({ onCall, onNavigate, onClose, onLocation, onHost, allow });
  handlers.current = { onCall, onNavigate, onClose, onLocation, onHost, allow };

  const post = useCallback(
    (message: unknown) => {
      const frame = frameRef.current;
      if (frame?.contentWindow == null) return;
      // Targeted at the extension's own origin, so a frame that
      // navigated elsewhere stops receiving what was meant for it.
      frame.contentWindow.postMessage(message, origin);
    },
    [origin],
  );

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const frame = frameRef.current;
      if (frame == null || event.source !== frame.contentWindow) return;
      if (event.origin !== origin) return;
      if (!isGuestMessage(event.data)) return;
      const message: GuestMessage = event.data;
      if (message.nonce !== nonce) return;

      const {
        onCall: call,
        onNavigate: navigate,
        onClose: close,
        onLocation: located,
        onHost: ask,
        allow: granted,
      } = handlers.current;

      if (message.kind === "host") {
        const { id, op, path, window: target, params: args, value, signals: wires } =
          message;
        const refuse = (code: string, text: string) =>
          post({
            igni: BRIDGE_VERSION,
            kind: "host-reply",
            id,
            ok: false,
            error: { code, message: text },
          });
        if (ask === undefined) {
          refuse("not_found", "this window does not reach the host model");
          return;
        }
        void ask({ id, op, path, window: target, params: args, value, signals: wires }).then(
          (reply) =>
            post({
              igni: BRIDGE_VERSION,
              kind: "host-reply",
              id,
              ok: reply.ok,
              result: reply.result,
              error: reply.error,
            }),
          (error: unknown) =>
            refuse("failed", error instanceof Error ? error.message : String(error)),
        );
        return;
      }

      if (message.kind === "navigate") {
        navigate?.(message.to);
        return;
      }
      if (message.kind === "close") {
        close?.();
        return;
      }
      if (message.kind === "location") {
        located?.(message.state);
        return;
      }

      if (!methodAllowed(message.method, granted)) {
        // Named, because the reader is the extension's author and the
        // fix is a manifest line. The frame learns only about itself.
        post({
          igni: BRIDGE_VERSION,
          kind: "result",
          id: message.id,
          ok: false,
          error:
            `'${extensionId}' is not permitted to call '${message.method}'. ` +
            `Add it to the extension's permissions and reinstall.`,
        });
        return;
      }

      void call(message.method, message.args).then(
        (value) =>
          post({
            igni: BRIDGE_VERSION,
            kind: "result",
            id: message.id,
            ok: true,
            value,
          }),
        (error: unknown) =>
          post({
            igni: BRIDGE_VERSION,
            kind: "result",
            id: message.id,
            ok: false,
            error: error instanceof Error ? error.message : String(error),
          }),
      );
    }

    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [extensionId, nonce, origin, post]);

  /**
   * The `params` value the frame has been told about.
   *
   * `null` until `hello` goes out. The effect below uses it to tell
   * "the host changed the parameters" from "the frame has not been
   * greeted yet", which are the same render otherwise.
   */
  const delivered = useRef<Record<string, unknown> | null>(null);

  /**
   * The highest `n` this frame has been given, or `null` before
   * `hello`.
   *
   * Set at the greeting from whatever is already queued, and not
   * posted: a panel the user has just opened is not owed the
   * backlog it missed while it was closed. The same rule the
   * host's own watcher follows — a first observation is not a
   * change.
   */
  const deliveredUpTo = useRef<number | null>(null);
  // Mirrored so `onLoad` can read the queue without taking it as a
  // dependency and rebuilding the greeting on every event.
  const queued = useRef<NonNullable<ExtensionFrameProps["events"]>>([]);
  queued.current = events ?? [];

  // The same, for host-model signals.
  const signalsUpTo = useRef<number | null>(null);
  const queuedSignals = useRef<NonNullable<ExtensionFrameProps["signals"]>>([]);
  queuedSignals.current = signals ?? [];

  // The frame cannot be spoken to until it has loaded, and it cannot
  // do anything until it has the nonce, so `hello` goes on load.
  const onLoad = useCallback(() => {
    const sent = params ?? {};
    delivered.current = sent;
    const waiting = queued.current;
    deliveredUpTo.current =
      waiting.length === 0 ? 0 : waiting[waiting.length - 1].n;
    const waitingSignals = queuedSignals.current;
    signalsUpTo.current =
      waitingSignals.length === 0
        ? 0
        : waitingSignals[waitingSignals.length - 1].n;
    post({
      igni: BRIDGE_VERSION,
      kind: "hello",
      nonce,
      extensionId,
      params: sent,
      settings: settings ?? {},
      theme,
      // Read at send time, not at render time: a token defined in a
      // media query resolves to what the user is actually looking at
      // only once the document has settled.
      tokens: readThemeTokens(),
    });
  }, [extensionId, nonce, params, post, settings, theme]);

  // Parameters that change under a mounted frame.
  //
  // Without this they arrived exactly once, inside `hello`, and the
  // only way to deliver new ones was to rebuild the frame — which
  // throws away whatever the extension was holding. That is the
  // difference between a widget and a page: a page can be linked to,
  // and a link resolved while the frame is already up had nowhere to
  // land.
  //
  // Skipped before `hello`, because `hello` carries them, and
  // skipped when the value is the one already delivered, so a parent
  // re-render with an inline object literal does not look like a
  // change.
  useEffect(() => {
    if (delivered.current === null) return;
    const next = params ?? {};
    if (sameParams(delivered.current, next)) return;
    delivered.current = next;
    post({ igni: BRIDGE_VERSION, kind: "params", params: next });
  }, [params, post]);

  /*
    Everything the frame has not been given yet, in order.

    Keyed on `n` rather than on the payload: two identical events
    are two events, and a guest waiting for "the file changed
    again" must hear it again. Skipped before `hello`, because a
    frame that has not been greeted has no nonce and no listener.
  */
  useEffect(() => {
    const upTo = deliveredUpTo.current;
    if (events === undefined || upTo === null) return;
    const pending = events.filter((one) => one.n > upTo);
    if (pending.length === 0) return;
    deliveredUpTo.current = pending[pending.length - 1].n;
    for (const one of pending) {
      post({
        igni: BRIDGE_VERSION,
        kind: "event",
        topic: one.topic,
        payload: one.payload,
      });
    }
  }, [events, post]);

  /*
    Host-model signals, as events are: in order, nothing before
    `hello`, nothing the frame was not addressed.
  */
  useEffect(() => {
    const upTo = signalsUpTo.current;
    if (signals === undefined || upTo === null) return;
    const pending = signals.filter((one) => one.n > upTo);
    if (pending.length === 0) return;
    signalsUpTo.current = pending[pending.length - 1].n;
    for (const one of pending) {
      post({
        igni: BRIDGE_VERSION,
        kind: "signal",
        signal: one.signal,
        value: one.value,
      });
    }
  }, [signals, post]);

  // Theme changes after the handshake are their own message rather
  // than a reload: a panel that reloaded every time the user flipped
  // to light mode would lose whatever they were in the middle of.
  useEffect(() => {
    post({
      igni: BRIDGE_VERSION,
      kind: "theme",
      theme,
      tokens: readThemeTokens(),
    });
  }, [post, theme]);

  return (
    <iframe
      ref={frameRef}
      className="extension-frame"
      title={title ?? extensionId}
      src={src}
      sandbox={SANDBOX}
      onLoad={onLoad}
      // No `allow` attribute: no camera, microphone, geolocation or
      // any other permission-policy feature. A panel does not need
      // them, and the default is to grant nothing.
    />
  );
}
