import { useMemo, useRef, useState } from "react";

import type { IgniClient } from "../../protocol/client";

/**
 * Answering a comment, and deciding whether igni is being asked.
 *
 * The queue offered Accept, Mark resolved and Decline — three terminal
 * actions and no way to say anything back. Declining told the
 * commenter "rejected" with no reason attached, and a question could
 * only be answered by closing it. Mark resolved belongs to a thread
 * that is *finished*, which it cannot be if nobody has replied.
 *
 * **`@igni` is what makes a reply an instruction.** Its absence means
 * the message is between people, however much it reads like a request
 * — "we should ask igni to fix this" is two colleagues agreeing. An
 * agent that decided for itself when it was being addressed would turn
 * every aside in a shared thread into a possible edit.
 *
 * And when it is addressed, it *proposes*: the answer comes back as a
 * suggestion the existing Accept button applies. Nobody's document
 * changes because somebody typed a name.
 */

const AGENT = { handle: "igni", label: "igni", agent: true };

interface Replied {
  ok: boolean;
  mentioned: string[];
  unknown: string[];
  agent_replied: boolean;
  agent_body: string;
  message: string;
  error: string;
}

/** The local part of an email — the handle people actually type. */
export function handleOf(author: string): string {
  const text = String(author || "").trim().toLowerCase();
  if (!text) return "";
  const local = text.includes("@") ? text.split("@")[0] : text;
  return local.replace(/[^a-z0-9.\-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
}

/**
 * The handle being typed at the caret, if one is.
 *
 * Only an unfinished handle at the very end of what has been typed
 * counts. Re-opening the picker because the caret happens to sit after
 * an `@` written ten minutes ago would fight the person writing.
 */
export function pendingHandle(text: string, caret: number): string | null {
  const before = text.slice(0, caret);
  const match = /(?:^|[\s(])@([A-Za-z0-9._-]*)$/.exec(before);
  return match ? match[1] : null;
}

export function CommentReplyBox({
  client,
  commentId,
  participants,
  onReplied,
}: {
  client: IgniClient;
  commentId: string;
  /** Emails of everyone who has spoken in this thread. */
  participants: string[];
  onReplied: () => void;
}) {
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [caret, setCaret] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const people = useMemo(() => {
    const seen = new Set<string>();
    const out: { handle: string; label: string; agent?: boolean }[] = [];
    for (const author of participants) {
      const handle = handleOf(author);
      // `igni` is reserved: a person cannot take the handle that
      // decides whether a message is an instruction.
      if (!handle || handle === AGENT.handle || seen.has(handle)) continue;
      seen.add(handle);
      out.push({ handle, label: author });
    }
    out.push(AGENT);
    return out;
  }, [participants]);

  const typing = pendingHandle(body, caret);
  const matches =
    typing === null
      ? []
      : people.filter((p) => p.handle.startsWith(typing.toLowerCase())).slice(0, 6);

  const asksAgent = /(?:^|[\s(])@igni\b/i.test(body);

  const pick = (handle: string) => {
    const before = body.slice(0, caret).replace(/@[A-Za-z0-9._-]*$/, `@${handle} `);
    const next = before + body.slice(caret);
    setBody(next);
    // The caret belongs after the handle just completed, not where it
    // was: typing on would otherwise insert into the middle of it.
    window.requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(before.length, before.length);
      setCaret(before.length);
    });
  };

  const send = async () => {
    const text = body.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    setNote("");
    try {
      const r = await client.rpc<Replied>("artifact_reply_comment", {
        comment_id: commentId,
        body: text,
      });
      if (r?.error) {
        setError(r.error);
        return;
      }
      setBody("");
      // Named rather than counted. A reply addressed to `@dna` reaches
      // nobody, and saying "replied" would leave its author believing
      // they asked somebody a question.
      const parts: string[] = [r?.message || "Replied."];
      if (r?.unknown?.length) {
        parts.push(`No one here is called ${r.unknown.map((h) => `@${h}`).join(", ")}.`);
      }
      setNote(parts.join(" "));
      onReplied();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="comment-reply">
      <div className="comment-reply-field">
        <textarea
          ref={inputRef}
          className="comment-reply-input"
          aria-label="Reply to this comment"
          placeholder="Reply — @someone to address them, @igni to ask for a change"
          rows={2}
          value={body}
          disabled={busy}
          onChange={(e) => {
            setBody(e.target.value);
            setCaret(e.target.selectionStart ?? e.target.value.length);
          }}
          onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={(e) => {
            if (matches.length && e.key === "Enter" && !e.shiftKey) {
              // The picker is open: Enter completes the handle rather
              // than sending a half-typed name to nobody.
              e.preventDefault();
              pick(matches[0].handle);
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />

        {matches.length > 0 && (
          <ul className="comment-reply-picker" role="listbox">
            {matches.map((p) => (
              <li key={p.handle}>
                <button
                  type="button"
                  className={p.agent ? "comment-reply-pick is-agent" : "comment-reply-pick"}
                  onMouseDown={(e) => {
                    // Before blur, or the textarea loses the caret the
                    // completion is measured against.
                    e.preventDefault();
                    pick(p.handle);
                  }}
                >
                  <span className="comment-reply-handle">@{p.handle}</span>
                  {p.label !== p.handle && (
                    <span className="comment-reply-who">{p.label}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="comment-reply-foot">
        <button
          type="button"
          className="btn btn-xs btn-primary"
          disabled={busy || !body.trim()}
          onClick={() => void send()}
        >
          {busy ? "…" : asksAgent ? "Reply and ask igni" : "Reply"}
        </button>
        {/* Said before sending, not after. Whether a model runs is the
            difference between a note and an edit being proposed. */}
        <span className="comment-reply-hint">
          {asksAgent
            ? "igni will answer in the thread and may propose a change — nothing is applied until you accept it."
            : "Goes to the people in this thread."}
        </span>
      </div>

      {note && <p className="comment-reply-note">{note}</p>}
      {error && <p className="msg-error comment-reply-note">{error}</p>}
    </div>
  );
}
