/**
 * What is waiting on you, in a panel down the right.
 *
 * It was a page inside the artifacts panel, which made answering
 * "is anybody waiting on me" a navigation: leave whatever you were
 * doing, go to artifacts, find the page, and afterwards find your
 * way back. A question that gets asked between other pieces of work
 * should be answerable without abandoning them, so it opens over the
 * app and closes again, and what was underneath is still there.
 *
 * **The review itself is not reimplemented here.** Replying,
 * resolving and declining are the same acts wherever they are done,
 * and two copies of them would be two sets of rules about what
 * happens when a suggestion is accepted — which is the one place in
 * this feature where getting it wrong writes a version. This is the
 * frame; ``ArtifactCommentReview`` is the contents.
 */
import { useEffect } from "react";

import { CloseIcon } from "./Icons";
import { ArtifactCommentReview } from "./panels/ArtifactCommentReview";
import type { IgniClient } from "../protocol/client";

export function NotificationsPanel({
  client,
  onOpenArtifact,
  onClose,
}: {
  client: IgniClient;
  /** Open the document a comment is about. Closes this on the way —
   *  the comment was the reason for going, so leaving the panel over
   *  the document it asked about would cover the answer. */
  onOpenArtifact?: (id: string) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      {/* Dimmed rather than blocked. What is underneath is still the
          thing being worked on, and a panel that hid it would make
          reading a comment about a document mean losing sight of the
          document. */}
      <div className="notify-backdrop" onClick={onClose} />
      <aside
        className="notify-panel"
        role="dialog"
        aria-label="Notifications"
        aria-modal="false"
      >
        <header className="notify-head">
          <h2 className="notify-title">Notifications</h2>
          {/* No count here. The review below opens with one, beside
              the button that refreshes it — two of them, a line
              apart, would be one number disagreeing with itself the
              moment anything was resolved. */}
          <span className="notify-count" />
          <button className="icon-btn" title="Close (Esc)" onClick={onClose}>
            <CloseIcon />
          </button>
        </header>
        <div className="notify-body">
          <ArtifactCommentReview
            client={client}
            onOpenArtifact={(id) => {
              onOpenArtifact?.(id);
              onClose();
            }}
          />
        </div>
      </aside>
    </>
  );
}
