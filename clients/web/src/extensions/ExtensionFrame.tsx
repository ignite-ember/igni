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
 * The method check is not the last word. The frame is untrusted, so
 * the backend re-checks the same grant; this one exists so a refusal
 * is immediate and legible rather than a round trip. See
 * `docs/BUILD_PLAN.md` for where the backend half lives.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  BRIDGE_VERSION,
  type GuestMessage,
  isGuestMessage,
  methodAllowed,
} from "./protocol";
import { readThemeTokens } from "./tokens";

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
  /** Makes the backend call. The host's client, never handed to the frame. */
  onCall: (method: string, args: Record<string, unknown>) => Promise<unknown>;
  /** The frame asked the app to go somewhere. The app decides. */
  onNavigate?: (to: string) => void;
  /** The frame says it is finished. */
  onClose?: () => void;
  /** Launch parameters, passed through on `hello`. */
  params?: Record<string, unknown>;
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
  params,
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
  const handlers = useRef({ onCall, onNavigate, onClose, allow });
  handlers.current = { onCall, onNavigate, onClose, allow };

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
        allow: granted,
      } = handlers.current;

      if (message.kind === "navigate") {
        navigate?.(message.to);
        return;
      }
      if (message.kind === "close") {
        close?.();
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

  // The frame cannot be spoken to until it has loaded, and it cannot
  // do anything until it has the nonce, so `hello` goes on load.
  const onLoad = useCallback(() => {
    post({
      igni: BRIDGE_VERSION,
      kind: "hello",
      nonce,
      extensionId,
      params: params ?? {},
      theme,
      // Read at send time, not at render time: a token defined in a
      // media query resolves to what the user is actually looking at
      // only once the document has settled.
      tokens: readThemeTokens(),
    });
  }, [extensionId, nonce, params, post, theme]);

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
