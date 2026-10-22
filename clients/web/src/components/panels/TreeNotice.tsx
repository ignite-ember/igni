/**
 * What just happened, said over the page instead of inside a column.
 *
 * "Archived. It is out of the tree; anything built on it still
 * resolves." is a sentence about an action, and it used to render as
 * a paragraph at the top of the hierarchy — which pushed every folder
 * down by two lines to announce that one of them had gone. The column
 * is for the tree. This is for the news.
 *
 * Portalled to the body: the dock it is raised from is a fixed-width
 * column with its own stacking context, and a notice clipped to that
 * column would be the narrowest possible place to read a sentence.
 *
 * **A success goes away; a refusal does not.** Something that worked
 * needs acknowledging and forgetting. Something that did not needs
 * reading, and a banner that vanishes while somebody is still working
 * out what it said is worse than no banner at all.
 */
import { useEffect } from "react";
import { createPortal } from "react-dom";

/** How long a success stays up. Long enough for two clauses. */
const SETTLE_MS = 6000;

export interface TreeNoticeProps {
  /** The message. Nothing renders when it is empty. */
  text: string;
  /** A refusal rather than a result: stays until dismissed. */
  tone?: "done" | "refused";
  onDismiss: () => void;
}

export function TreeNotice({ text, tone = "done", onDismiss }: TreeNoticeProps) {
  useEffect(() => {
    if (!text || tone === "refused") return;
    const timer = window.setTimeout(onDismiss, SETTLE_MS);
    // Cleared when the message changes as well as on unmount, or a
    // second action inherits the remainder of the first one's timer
    // and its notice disappears early.
    return () => window.clearTimeout(timer);
  }, [text, tone, onDismiss]);

  if (!text) return null;

  return createPortal(
    <div className={`tree-notice tree-notice-${tone}`} role="status" aria-live="polite">
      <span className="tree-notice-text">{text}</span>
      <button
        type="button"
        className="tree-notice-close"
        onClick={onDismiss}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>,
    document.body,
  );
}
