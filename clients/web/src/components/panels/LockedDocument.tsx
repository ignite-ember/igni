/**
 * A document you were sent and cannot open.
 *
 * The title, who owns it, and a way to ask — in the place the text
 * would have been. Not an error page: nothing has gone wrong, and a
 * red box saying "403" tells somebody holding a link from a colleague
 * that the product is broken rather than that they need to ask for
 * something.
 *
 * **The title is the whole reason this page exists.** Without it the
 * answer to "what did Maya send me?" is a slug, and the request that
 * follows is "can I have access to, er, one of your documents". With
 * it the ask is a sentence.
 *
 * Asking is one button and an optional line. Requiring a reason turns
 * a two-second ask into a paragraph nobody writes, and an owner
 * deciding usually needs the name and the document rather than the
 * argument.
 */
import { useState } from "react";

import { MenuIcon } from "../MenuIcon";

export interface LockedState {
  open: boolean;
  title: string;
  owner_name: string;
  owner_email: string;
  asked: boolean;
  declined: boolean;
  error: string;
}

/** What the page says about where the request stands. */
export function standing(locked: LockedState): string {
  const owner = locked.owner_name || "the owner";
  if (locked.asked) return `${owner} has been asked. You will be able to open it once they agree.`;
  if (locked.declined) {
    // Said, not hidden. Silence after a refusal reads as the request
    // having been lost, and the person raises it again — which is the
    // one outcome the refusal was meant to prevent.
    return `${owner} declined an earlier request. You can ask again if something has changed.`;
  }
  return `${owner} can give you access.`;
}

export function LockedDocument({
  locked,
  busy,
  onAsk,
}: {
  locked: LockedState;
  busy: boolean;
  onAsk: (note: string) => void;
}) {
  const [note, setNote] = useState("");

  return (
    <article className="kb-doc kb-doc-locked">
      <header className="kb-doc-head">
        <div className="kb-doc-head-row">
          <h2 className="kb-doc-title">
            {/* The title is what this page is for, so it is drawn as
                the title — not as a subtitle under an apology. */}
            {locked.title || "A shared document"}
          </h2>
        </div>
        <p className="kb-doc-attribution">
          <MenuIcon name="lock" />
          {locked.owner_email ? ` ${locked.owner_email}` : ""}
        </p>
      </header>

      <div className="doc-locked-body">
        <p className="doc-locked-said">{standing(locked)}</p>

        {locked.asked ? (
          // Nothing to press. A button that would raise the same
          // request again is a button that fills somebody's queue.
          <p className="doc-locked-waiting">Waiting on them.</p>
        ) : (
          <form
            className="doc-locked-ask"
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy) onAsk(note);
            }}
          >
            <label className="doc-locked-label" htmlFor="doc-locked-note">
              Say why, if it helps
            </label>
            <textarea
              id="doc-locked-note"
              className="doc-comment-input"
              rows={2}
              value={note}
              disabled={busy}
              placeholder="Optional — what you need it for."
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="doc-locked-actions">
              <button type="submit" className="btn btn-sm btn-primary" disabled={busy}>
                {busy ? "…" : "Request access"}
              </button>
            </div>
          </form>
        )}

        {locked.error && <p className="msg-error">{locked.error}</p>}
      </div>
    </article>
  );
}
