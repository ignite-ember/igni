import { useCallback, useEffect, useMemo, useState } from "react";
import type { IgniClient } from "../../protocol/client";

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

  if (rows === null) return <div className="kb-hint">Checking…</div>;

  return (
    <div className="comment-review">
      <div className="comment-review-head">
        <span className="kb-hint">
          {rows.length === 0
            ? "Nothing is waiting on you."
            : `${rows.length} comment${rows.length === 1 ? "" : "s"} waiting`}
        </span>
        <button type="button" onClick={sync} disabled={syncing}>
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
            <article key={comment.comment_id} className="comment-review-item">
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

              <div className="comment-review-actions">
                {comment.suggested_text ? (
                  <button
                    type="button"
                    disabled={busy === comment.comment_id}
                    onClick={() =>
                      act(
                        comment,
                        "accepted",
                        drafts[comment.comment_id] ?? comment.suggested_text,
                      )
                    }
                  >
                    Accept and write v{/* the next one */ comment.n + 1}
                  </button>
                ) : null}
                <button
                  type="button"
                  disabled={busy === comment.comment_id}
                  onClick={() => act(comment, "resolved")}
                >
                  Mark resolved
                </button>
                <button
                  type="button"
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
