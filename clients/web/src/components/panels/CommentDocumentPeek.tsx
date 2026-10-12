import { useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { IgniClient } from "../../protocol/client";
import { findText } from "./DocumentComment";

/**
 * The document a comment is about, at the passage it is about.
 *
 * The review queue shows a one-line quote and nothing around it. That
 * is enough to read the comment and not enough to judge it: "add a
 * sentence saying where the denylist lives" is a different decision
 * depending on whether the surrounding paragraph already half says
 * it, and the only way to find out was to leave the queue, open the
 * document, and find the line by eye.
 *
 * **Head first, and the version commented on only as a fallback.**
 * The decision writes to head — accepting appends after whatever is
 * current — so head is the text the reviewer is actually acting on.
 * When the quote is not in head the passage has been rewritten or
 * removed since, and that is the single most useful thing this panel
 * can say; it says it, then shows the older version so the comment
 * can still be read against what its author saw.
 *
 * Not a summary and not a judgement — the document, scrolled. Nothing
 * here asks a model anything.
 */

interface DocFull {
  content: string;
  n?: number;
  error?: string;
}

/** Which version answered, and whether the quoted passage is in it. */
interface Peek {
  content: string;
  /** The version on screen. */
  n: number;
  /** False when head no longer contains the quote. */
  atHead: boolean;
  found: boolean;
  error: string;
}

/**
 * Fetch head, and fall back to the commented version if the passage
 * has gone.
 *
 * Two requests in the worst case and one in the common one. Cached by
 * the caller, so hovering the same document twice costs nothing.
 */
async function readAround(
  client: IgniClient,
  artifactId: string,
  quote: string,
  headN: number,
  commentN: number,
): Promise<Peek> {
  const squash = (s: string) => s.replace(/\s+/g, " ").trim();
  const has = (body: string) => !quote.trim() || squash(body).includes(squash(quote));

  const read = async (version: number) =>
    client.rpc<DocFull>("artifact_get", { id: artifactId, version });

  try {
    const head = await read(0);
    if (head?.error) return { content: "", n: 0, atHead: true, found: false, error: head.error };
    const headBody = head?.content ?? "";
    if (has(headBody)) {
      return { content: headBody, n: head?.n ?? headN, atHead: true, found: true, error: "" };
    }
    // Gone from head. Show where it *was*, and say so.
    if (commentN && commentN !== (head?.n ?? headN)) {
      const older = await read(commentN);
      const olderBody = older?.content ?? "";
      if (olderBody) {
        return {
          content: olderBody,
          n: older?.n ?? commentN,
          atHead: false,
          found: has(olderBody),
          error: "",
        };
      }
    }
    return { content: headBody, n: head?.n ?? headN, atHead: true, found: false, error: "" };
  } catch (e) {
    return {
      content: "",
      n: 0,
      atHead: true,
      found: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

export function CommentDocumentPeek({
  client,
  artifactId,
  documentName,
  quote,
  commentN,
  headN,
  anchor,
  cache,
  onEnter,
  onLeave,
}: {
  client: IgniClient;
  artifactId: string;
  documentName: string;
  quote: string;
  commentN: number;
  headN: number;
  /** The card this is about, so the panel can sit beside it. */
  anchor: DOMRect;
  /** Shared across rows: the same document is hovered many times. */
  cache: Map<string, Peek>;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const key = `${artifactId}:${headN}:${commentN}:${quote.slice(0, 60)}`;
  const [peek, setPeek] = useState<Peek | null>(() => cache.get(key) ?? null);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const stored = cache.get(key);
    if (stored) {
      setPeek(stored);
      return;
    }
    let cancelled = false;
    void readAround(client, artifactId, quote, headN, commentN).then((result) => {
      cache.set(key, result);
      if (!cancelled) setPeek(result);
    });
    return () => {
      cancelled = true;
    };
  }, [client, artifactId, quote, headN, commentN, key, cache]);

  /**
   * Put the quoted passage on screen and light it.
   *
   * A layout effect rather than an animation frame: this panel is
   * opened by a hover and closed by leaving, so it can easily be gone
   * before the next frame — and Chrome does not run frames in a
   * background tab at all.
   */
  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body || !peek?.content || !quote.trim()) return;
    const range = findText(body, quote);
    const target = range?.startContainer.parentElement;
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ block: "center" });
    }

    const highlights =
      typeof CSS === "undefined"
        ? undefined
        : (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
    const HighlightCtor = (
      window as unknown as { Highlight?: new (...r: Range[]) => unknown }
    ).Highlight;
    if (!highlights || !HighlightCtor || !range) return;
    highlights.set("comment-peek", new HighlightCtor(range));
    return () => {
      highlights.delete("comment-peek");
    };
  }, [peek, quote]);

  // Beside the card when there is room, and on its other side when
  // there is not. Clamped so the panel never opens off the screen,
  // which on the bottom rows is where it would otherwise go.
  const width = 420;
  const height = Math.min(520, window.innerHeight - 24);
  const right = anchor.right + 16;
  const left = right + width < window.innerWidth ? right : Math.max(12, anchor.left - width - 16);
  const top = Math.min(Math.max(12, anchor.top), Math.max(12, window.innerHeight - height - 12));

  return (
    <aside
      className="comment-peek"
      style={{ left, top, width, height }}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      role="dialog"
      aria-label={`${documentName} around the commented passage`}
    >
      <header className="comment-peek-head">
        <strong className="comment-peek-name">{documentName || artifactId}</strong>
        {peek ? <span className="comment-peek-where">v{peek.n}</span> : null}
      </header>

      {peek === null ? (
        <p className="kb-hint comment-peek-note">Reading the document…</p>
      ) : peek.error ? (
        <p className="msg-error comment-peek-note">{peek.error}</p>
      ) : (
        <>
          {/* The finding, not a footnote. Acting on a quote that is
              no longer in the document is the mistake this queue is
              most likely to invite, and "the document has moved on
              since" — which every row says — cannot tell a passage
              that survived from one that is gone.

              Three outcomes, not two. Falling back and *still* not
              finding the passage is its own answer: the first cut
              said "showing v2, where the comment was left" in that
              case too, which points a reviewer at a version that
              does not contain it either. */}
          {!peek.atHead && peek.found && (
            <p className="comment-peek-note comment-peek-gone">
              This passage is not in v{headN} any more. Showing v{peek.n}, where the
              comment was left.
            </p>
          )}
          {!peek.atHead && !peek.found && (
            <p className="comment-peek-note comment-peek-gone">
              This passage is not in v{headN}, and could not be located in v{peek.n}
              {" "}either.
            </p>
          )}
          {peek.atHead && !peek.found && quote.trim() ? (
            <p className="comment-peek-note comment-peek-gone">
              The quoted passage could not be located in this version.
            </p>
          ) : null}
          <div className="comment-peek-body markdown-body" ref={bodyRef}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {peek.content || "_(empty)_"}
            </ReactMarkdown>
          </div>
        </>
      )}
    </aside>
  );
}
