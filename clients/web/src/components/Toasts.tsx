/**
 * Toasts — top-right notification stack.
 *
 * Scheduled tasks (and other project-level async events) shouldn't
 * inject into whichever chat the user happens to be in — that buries
 * the result in an unrelated conversation. They land here instead,
 * outside the chat scroll, persist across conversation switches, and
 * auto-dismiss after a few seconds.
 *
 * The list is the window model's ``ui.toasts`` — igni's own toasts and
 * an extension's (``host.ui.toast``) alike. This component renders it
 * and reports the person dismissing one or clicking through to where it
 * points; the closing animation is its own.
 */

import { useEffect, useState } from "react";

import type { ModelToast } from "../clientState";
import type { PageRoute } from "../chat/pageStack";

/** Auto-dismiss after this many ms. */
export const TOAST_TTL_MS = 8000;

export function Toasts({
  items,
  onDismiss,
  onOpen,
}: {
  items: readonly ModelToast[];
  onDismiss: (id: string) => void;
  /** Clicking a toast that points somewhere goes there. */
  onOpen?: (target: PageRoute) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="toasts" role="region" aria-label="Notifications">
      {items.map((t) => (
        <ToastCard
          key={t.id}
          toast={t}
          onDismiss={() => onDismiss(t.id)}
          onOpen={onOpen}
        />
      ))}
    </div>
  );
}

function ToastCard({
  toast,
  onDismiss,
  onOpen,
}: {
  toast: ModelToast;
  onDismiss: () => void;
  onOpen?: (target: PageRoute) => void;
}) {
  // The exit animation, between the person's click (or the timer) and
  // the toast leaving the model.
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => {
      setClosing(true);
      // Wait for the exit animation, then drop from the list.
      window.setTimeout(onDismiss, 200);
    }, TOAST_TTL_MS);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast.id]);
  return (
    <div className={`toast${closing ? " is-closing" : ""}`}>
      <button
        type="button"
        className="toast-body-btn"
        onClick={() => {
          if (toast.target) onOpen?.(toast.target);
          setClosing(true);
          window.setTimeout(onDismiss, 150);
        }}
      >
        <div className="toast-title">{toast.text}</div>
        {toast.body && <div className="toast-body">{toast.body}</div>}
      </button>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss"
        onClick={(e) => {
          e.stopPropagation();
          setClosing(true);
          window.setTimeout(onDismiss, 150);
        }}
      >
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor"
          strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  );
}
