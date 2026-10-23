/**
 * "It saved." — the message every extension needs and none had.
 *
 * EXTENSIBILITY §5 names `notify` in the list of bridge APIs that do
 * not exist. A specified-and-absent API is worse than a missing one,
 * because a partner designs against it and finds out on demo day.
 *
 * This is the in-frame half: an extension's own toasts, in its own
 * frame. It is not `bridge.notify` — that would put a message in
 * igni's chrome, outside the frame, and needs a host envelope that
 * does not exist yet. The distinction matters because they are not
 * interchangeable: a toast about something inside this panel belongs
 * inside this panel, and only something the user must see after
 * navigating away belongs to the host.
 *
 * **Announced, not just drawn.** The stack is a polite live region,
 * so a toast reaches somebody who is not looking at that corner. A
 * `danger` toast is assertive instead: it is the one case where
 * interrupting is correct.
 *
 * Toasts do not stack indefinitely and do not auto-dismiss when they
 * carry an action — a message that disappears while you are reaching
 * for its button is a message that was never actionable.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type HTMLAttributes,
  type ReactNode,
} from "react";

import type { Tone } from "./primitives";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** How long a toast stays, when it stays at all. */
const DEFAULT_MS = 5_000;

/** Beyond this, the oldest goes. A wall of toasts is not a message. */
const MAX_VISIBLE = 4;

export interface ToastOptions {
  tone?: Tone;
  /**
   * Milliseconds before it goes. `null` keeps it until dismissed,
   * which is the default when there is an `action`.
   */
  duration?: number | null;
  /** One thing to do about it. Keeps the toast until dismissed. */
  action?: ReactNode;
}

export interface Toast extends ToastOptions {
  id: string;
  message: ReactNode;
}

export interface ToastApi {
  /** Show one. Returns its id, so it can be dismissed early. */
  show: (message: ReactNode, options?: ToastOptions) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let counter = 0;

/**
 * Wrap the part of the app that raises toasts.
 *
 * Renders the stack itself, so there is nothing to place and no way
 * to forget to place it.
 */
export function ToastHost({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: ReactNode, options: ToastOptions = {}) => {
      const id = `toast-${++counter}`;
      // A toast with something to do stays until it is dealt with.
      // Auto-dismissing it means the button can vanish mid-reach.
      const duration =
        options.duration !== undefined
          ? options.duration
          : options.action !== undefined
            ? null
            : DEFAULT_MS;

      setToasts((current) =>
        [...current, { ...options, id, message }].slice(-MAX_VISIBLE),
      );

      if (duration !== null) {
        timers.current.set(
          id,
          setTimeout(() => dismiss(id), duration),
        );
      }
      return id;
    },
    [dismiss],
  );

  const clear = useCallback(() => {
    for (const timer of timers.current.values()) clearTimeout(timer);
    timers.current.clear();
    setToasts([]);
  }, []);

  // Timers outlive the component otherwise, and fire `setToasts` on
  // something unmounted.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({ show, dismiss, clear }),
    [show, dismiss, clear],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastList toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastList({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}) {
  return (
    <div className="ui-toasts">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className="ui-toast"
          data-tone={toast.tone ?? "neutral"}
          // A failure interrupts; everything else waits for a pause.
          role={toast.tone === "danger" ? "alert" : "status"}
          aria-live={toast.tone === "danger" ? "assertive" : "polite"}
        >
          <div className="ui-toast-body">
            {toast.message}
            {toast.action}
          </div>
          <button
            type="button"
            className="ui-toast-dismiss"
            // Named, because "×" is announced as "times" or as
            // nothing, and a stack of them is unnavigable.
            aria-label="Dismiss notification"
            onClick={() => onDismiss(toast.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

/**
 * Raise a toast.
 *
 * Throws outside a {@link ToastHost} rather than returning a no-op.
 * A silent no-op here means a save that reports success by showing
 * nothing, which looks exactly like a save that did not happen.
 */
export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (api === null) {
    throw new Error(
      "useToast was called outside a <ToastHost>. Wrap your app in one — " +
        "without it a toast would be silently dropped, which looks the " +
        "same as the action never happening.",
    );
  }
  return api;
}

export interface BannerProps extends HTMLAttributes<HTMLDivElement> {
  tone?: Tone;
  /** A dismiss affordance appears when this is given. */
  onDismiss?: () => void;
}

/**
 * A condition that is still true.
 *
 * Not a toast: a toast is about something that just happened and
 * goes away, a banner is about a state — offline, read-only, licence
 * expiring — and stays until the state does not.
 */
export function Banner({
  tone = "neutral",
  onDismiss,
  children,
  className,
  ...rest
}: BannerProps) {
  return (
    <div
      {...rest}
      className={cx("ui-banner", className)}
      data-tone={tone}
      role={tone === "danger" ? "alert" : "status"}
    >
      <div className="ui-banner-body">{children}</div>
      {onDismiss === undefined ? null : (
        <button
          type="button"
          className="ui-toast-dismiss"
          aria-label="Dismiss message"
          onClick={onDismiss}
        >
          ×
        </button>
      )}
    </div>
  );
}
