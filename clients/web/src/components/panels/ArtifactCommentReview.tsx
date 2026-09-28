import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { IgniClient } from "../../protocol/client";
import { CommentDocumentPeek } from "./CommentDocumentPeek";
import { CommentReplyBox } from "./CommentReplyBox";

/**
 * Comments waiting on you, across every document you have published.
 *
 * Cross-document on purpose. A comment is somebody asking a question
 * of a person, not of a document, and a queue that made you open each
 * document to discover whether anything was waiting would be a queue
 * you stop opening.
 *
 * The rows are read from the **local graph**, not the API. That is
 * the whole reason comments are projected on sync: this screen
 * answers with the network down, using the same store that answers
 * staleness and lineage.
 *
 * Unlike the stale-review panel, this one does write — and only in
 * one direction. Accepting a suggestion appends a version and tells
 * the server which version carried it, in that order, so a closed
 * thread never points at a version that was not written. Rejecting
 * and resolving touch nothing but the status: a rejected comment is
 * the record of a decision, and deleting it invites the same comment
 * again next week.
 */

interface CommentRow {
  comment_id: string;
  artifact_id: string;
  document_name: string;
  n: number;
  thread_id: string;
  quote: string;
  body: string;
  suggested_text: string;
  status: string;
  resolved_in_n: number;
  author: string;
  at: string;
  behind_head: boolean;
  /** Where the document is now. Accepting appends after *this*. */
  head_n: number;
}

interface Resolved {
  ok: boolean;
  status: string;
  resolved_in_n: number;
  message: string;
}

export function ArtifactCommentReview({
  client,
  onOpenArtifact,
}: {
  client: IgniClient;
  onOpenArtifact?: (id: string) => void;
}) {
  const [rows, setRows] = useState<CommentRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [syncing, setSyncing] = useState(false);
  /**
   * The comment whose document is being previewed, and where its card
   * sits.
   *
   * A one-line quote is enough to read a comment and not enough to
   * judge one — whether a paragraph already half says what is being
   * asked for decides the answer, and finding that out meant leaving
   * the queue. Hovering shows the document, scrolled to the passage.
   */
  const [peeking, setPeeking] = useState<{ row: CommentRow; at: DOMRect } | null>(null);
  /** Documents already read, so hovering the same one again is free. */
  const peekCache = useRef(new Map<string, never>());
  /**
   * Opening and closing are both delayed, for opposite reasons.
   *
   * Opening, because a pointer crossing the list on its way somewhere
   * else should not fire four fetches and four panels. Closing,
   * because the panel sits beside the card with a gap between them,
   * and closing the instant the pointer leaves the card makes the
   * panel unreachable.
   */
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const peekAt = useCallback((row: CommentRow, element: HTMLElement) => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    const at = element.getBoundingClientRect();
    hoverTimer.current = setTimeout(() => setPeeking({ row, at }), 220);
  }, []);

  const endPeek = useCallback(() => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => setPeeking(null), 160);
  }, []);

  const holdPeek = useCallback(() => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
  }, []);

  useEffect(
    () => () => {
      if (hoverTimer.current) clearTimeout(hoverTimer.current);
    },
    [],
  );

  /**
   * Refresh the queue.
   *
   * Deliberately does **not** clear the error on success. Every action
   * reloads afterwards, including one that failed — and a reload that
   * wiped the message would erase "version 3 was written but the
   * server was not told" a frame after showing it, leaving a queue
   * that looks like nothing went wrong. Clearing belongs to whatever
   * succeeded, not to the read that follows it.
   */
  const load = useCallback(async () => {
    try {
      const result = await client.rpc<CommentRow[]>("artifact_comments", {
        open_only: true,
      });
      setRows(result ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const sync = async () => {
    setSyncing(true);
    try {
      await client.rpc<number>("artifact_sync_comments");
      setError("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const act = async (comment: CommentRow, status: string, content?: string) => {
    setBusy(comment.comment_id);
    try {
      const result = await client.rpc<Resolved>("artifact_resolve_comment", {
        comment_id: comment.comment_id,
        status,
        ...(content ? { content } : {}),
      });
      if (!result?.ok) {
        // The message matters more than the failure here: "the
        // version was written but the server was not told" is a
        // different problem from "nothing happened", and only one of
        // them means you should try again.
        setError(result?.message || "Could not resolve that comment.");
        await load();
        return;
      }
      setError("");
      setDrafts((d) => {
        const next = { ...d };
        delete next[comment.comment_id];
        return next;
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const byDocument = useMemo(() => groupByDocument(rows ?? []), [rows]);

  /**
   * Who has spoken in each thread, so a reply can offer them.
   *
   * Built from the rows already loaded rather than fetched: the queue
   * holds every comment on every document, so the participants of a
   * thread are already on screen.
   */
  const authorsByThread = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const row of rows ?? []) {
      const key = row.thread_id || row.comment_id;
      const seen = out.get(key) ?? [];
      if (row.author && !seen.includes(row.author)) seen.push(row.author);
      out.set(key, seen);
    }
    return out;
  }, [rows]);

  if (rows === null) return <div className="kb-hint">Checking…</div>;

  return (
    <div className="comment-review">
      <div className="comment-review-head">
        <span className="kb-hint">
          {rows.length === 0
            ? "Nothing is waiting on you."
            : `${rows.length} comment${rows.length === 1 ? "" : "s"} waiting`}
        </span>
        <button type="button" className="btn btn-sm" onClick={sync} disabled={syncing}>
          {syncing ? "Checking…" : "Check for new"}
        </button>
      </div>

      {error ? <div className="msg-error">{error}</div> : null}

      {byDocument.map(([document, comments]) => (
        <section key={document} className="comment-review-doc">
          <h3 className="comment-review-doc-name">
            {onOpenArtifact ? (
              <button
                type="button"
                className="comment-review-link"
                onClick={() => onOpenArtifact(comments[0].artifact_id)}
              >
                {document || "untitled"}
              </button>
            ) : (
              (document || "untitled")
            )}
          </h3>

          {comments.map((comment) => (
            <article
              key={comment.comment_id}
              className="comment-review-item"
              onMouseEnter={(e) => peekAt(comment, e.currentTarget)}
              onMouseLeave={endPeek}
              // Focus counts as hover, so the preview is reachable
              // without a pointer.
              onFocus={(e) => peekAt(comment, e.currentTarget)}
              onBlur={endPeek}
            >
              {comment.quote ? (
                <blockquote className="comment-review-quote">{comment.quote}</blockquote>
              ) : null}

              <p className="comment-review-body">{comment.body}</p>

              <p className="comment-review-meta">
                {comment.author || "someone"} · on v{comment.n}
                {comment.behind_head ? (
                  /* The passage may have been rewritten since. Said
                     out loud, because acting on a quote that no
                     longer appears in the document is the mistake
                     this screen is most likely to invite. */
                  <span className="comment-review-warn">
                    {" "}
                    · the document has moved on since
                  </span>
                ) : null}
              </p>

              {comment.suggested_text ? (
                <>
                  <p className="kb-hint">Suggested replacement:</p>
                  <textarea
                    className="comment-review-suggestion"
                    aria-label={`Suggested replacement for ${comment.comment_id}`}
                    value={drafts[comment.comment_id] ?? comment.suggested_text}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [comment.comment_id]: e.target.value }))
                    }
                    rows={5}
                  />
                  <p className="kb-hint">
                    Accepting writes a new version of {document || "this document"} with
                    exactly this text.
                  </p>
                </>
              ) : null}

              {/* Before the verdict buttons, because answering is the
                  ordinary case and closing is the exception. Mark
                  resolved is for a thread that is finished, not for
                  one nobody replied to. */}
              <CommentReplyBox
                client={client}
                commentId={comment.comment_id}
                participants={authorsByThread.get(comment.thread_id || comment.comment_id) ?? []}
                onReplied={load}
              />

              <div className="comment-review-actions">
                {comment.suggested_text ? (
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    disabled={busy === comment.comment_id}
                    onClick={() =>
                      act(
                        comment,
                        "accepted",
                        drafts[comment.comment_id] ?? comment.suggested_text,
                      )
                    }
                  >
                    {/* Counted from *head*, not from the version
                        commented on. Accepting revises the document,
                        and a revision appends after whatever is
                        current — so on a comment left on v2 of a
                        document since taken to v5, this said "write
                        v3" and wrote v6. A button that writes to the
                        store must not be wrong about what it writes. */}
                    Accept and write v{Math.max(comment.head_n, comment.n) + 1}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={busy === comment.comment_id}
                  onClick={() => act(comment, "resolved")}
                >
                  Mark resolved
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-quiet"
                  disabled={busy === comment.comment_id}
                  onClick={() => act(comment, "rejected")}
                >
                  Decline
                </button>
              </div>
            </article>
          ))}
        </section>
      ))}

      {peeking && (
        <CommentDocumentPeek
          client={client}
          artifactId={peeking.row.artifact_id}
          documentName={peeking.row.document_name}
          quote={peeking.row.quote}
          commentN={peeking.row.n}
          headN={peeking.row.head_n}
          anchor={peeking.at}
          cache={peekCache.current as never}
          onEnter={holdPeek}
          onLeave={endPeek}
        />
      )}
    </div>
  );
}

/**
 * Group rows by document, preserving the order they arrived in.
 *
 * Insertion order rather than alphabetical: the rows come back
 * oldest-first, so the document somebody has been waiting on longest
 * stays at the top.
 */
export function groupByDocument(rows: CommentRow[]): [string, CommentRow[]][] {
  const grouped = new Map<string, CommentRow[]>();
  for (const row of rows) {
    const existing = grouped.get(row.document_name);
    if (existing) existing.push(row);
    else grouped.set(row.document_name, [row]);
  }
  return [...grouped.entries()];
}
