import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { IgniClient } from "../../protocol/client";

/**
 * Talking to the agent about the document you are reading, on the page.
 *
 * This is the local half of the comment model, and it is a different
 * act from its twin in the hosted app even though both are called
 * "commenting". On the web you are talking to a colleague, so the
 * answer is a reply. Here the agent is the only other party in the
 * room, so the answer is a new version.
 *
 * The exchange stays *here*, in a rail beside the document. Sending
 * the turn used to jump to the chat, which took you off the document
 * you were commenting on to watch a reply about it. Putting the
 * answer under the article instead was no better: a comment is about
 * a *passage*, and a reader who scrolls past the whole document to
 * read the reply, then scrolls back to find the sentence it was
 * about, is doing the join by hand. Beside it, both are in view.
 *
 * **A thread is the unit, not a message.** Anything written into a
 * thread stays in it — the composer at its foot is part of it, so
 * continuing a conversation is the default and starting a new one
 * takes a deliberate act. The version of this that offered a Reply
 * button made the opposite true, and every remark started a thread
 * of its own: eleven comments, eleven threads, and a history block
 * that was never once populated.
 *
 * The passage and its thread point at each other in both directions.
 * Click a thread and the document scrolls to the sentence; click the
 * sentence and its thread comes forward. Without that the rail is a
 * column of quotes next to a page of prose and the reader does the
 * matching.
 */

export interface CommentPosted {
  ok: boolean;
  comment_id: string;
  thread_id: string;
  /** The turn text, assembled by the backend from the whole thread. */
  prompt: string;
  /** Earlier comments folded in as context. */
  history_count: number;
  message: string;
}

/** One turn in a thread — mine, or the answer to it. */
export interface Turn {
  key: string;
  who: "me" | "agent";
  body: string;
  /** What the agent is doing, while nothing has come back yet. */
  activity: string;
  phase: "running" | "done" | "failed";
  error: string;
}

/** A conversation about one passage, or about the document. */
export interface Thread {
  key: string;
  /** The server's id. Empty until the first turn lands. */
  id: string;
  quote: string;
  /** Where the passage sat when the thread was started. A hint for
   *  picking between repeated lines, never the anchor. */
  quoteStart: number;
  turns: Turn[];
  error: string;
}

/** How far a context menu stays from the window edge. */
const EDGE_MARGIN = 12;

interface Anchor {
  x: number;
  y: number;
}

/** Whether a thread is waiting on the agent. */
export function isRunning(thread: Thread): boolean {
  return thread.turns.some((turn) => turn.phase === "running");
}

/**
 * Where a position inside ``root`` falls in its flattened text.
 *
 * Recorded with a comment so a document containing the same sentence
 * twice can tell the two apart. It is a *hint*, never the anchor:
 * everything below an edit shifts, so the number is wrong the moment
 * anything above it changes. Matching is still by text; this only
 * decides which match.
 */
export function offsetWithin(root: HTMLElement, node: Node, nodeOffset: number): number {
  // Measured with a Range rather than by walking text nodes. A
  // selection's start is not always *in* a text node — double-click a
  // word or select a whole element and the container is the element,
  // with the offset counting children rather than characters. Walking
  // text nodes never finds that container, and the version of this
  // that did fell through to returning the raw offset: every such
  // selection recorded position 0, which is the earliest possible
  // answer and therefore useless as a tie-break between repeated
  // lines — the exact thing the offset exists for.
  // Checked, not caught: ``setEnd`` does not throw for a node that is
  // merely elsewhere in the document — it collapses the range and
  // answers 0, which is a real position and would be believed.
  if (!root.contains(node)) return -1;
  const probe = document.createRange();
  probe.selectNodeContents(root);
  probe.setEnd(node, nodeOffset);
  return probe.toString().length;
}

export function findText(root: HTMLElement, needle: string, near = -1): Range | null {
  // An empty needle would "match" at position 0 and yield a
  // degenerate range covering the first character — a highlight on a
  // passage nobody asked about. The caller guards this, but a
  // function that answers a meaningless question with a plausible
  // range is a trap for the next caller.
  if (!needle.trim()) return null;

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  const starts: number[] = [];
  let flat = "";
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    nodes.push(node as Text);
    starts.push(flat.length);
    flat += node.textContent ?? "";
  }

  const squash = (s: string) => s.replace(/\s+/g, " ");
  const haystack = squash(flat);
  const target = squash(needle);

  // Every occurrence, so a repeated line can be told apart by where
  // it sits rather than by being first.
  const hits: number[] = [];
  for (let i = haystack.indexOf(target); i >= 0; i = haystack.indexOf(target, i + 1)) {
    hits.push(i);
  }
  if (!hits.length) return null;

  // Nearest to where the passage was when the comment was written.
  // With no hint, or after an edit moved everything, the first is as
  // good an answer as any — and is at least stable.
  const at =
    near < 0
      ? hits[0]
      : hits.reduce((best, i) => (Math.abs(i - near) < Math.abs(best - near) ? i : best), hits[0]);

  // Map a position in the squashed text back to the original by
  // counting non-collapsed characters.
  const toOriginal = (target: number) => {
    let seen = 0;
    let previousWasSpace = false;
    for (let i = 0; i < flat.length; i++) {
      const isSpace = /\s/.test(flat[i]);
      if (!isSpace || !previousWasSpace) {
        if (seen === target) return i;
        seen++;
      }
      previousWasSpace = isSpace;
    }
    return flat.length;
  };

  const from = toOriginal(at);
  const to = toOriginal(at + target.length);

  const locate = (offset: number): [Text, number] => {
    let i = starts.findIndex((s, k) => offset < s + (nodes[k].textContent?.length ?? 0));
    if (i < 0) i = nodes.length - 1;
    return [nodes[i], Math.max(0, offset - starts[i])];
  };

  try {
    const range = document.createRange();
    const [startNode, startOffset] = locate(from);
    const [endNode, endOffset] = locate(Math.max(from, to - 1));
    range.setStart(startNode, startOffset);
    range.setEnd(endNode, Math.min(endOffset + 1, endNode.textContent?.length ?? 0));
    return range;
  } catch {
    return null;
  }
}

/** Whether a quoted passage is still in the document as it stands. */
export function quoteStillPresent(root: HTMLElement | null, quote: string): boolean {
  if (!root || !quote.trim()) return true;
  const squash = (s: string) => s.replace(/\s+/g, " ").trim();
  return squash(root.textContent ?? "").includes(squash(quote));
}

/**
 * Which thread's passage a point in the document falls inside.
 *
 * Highlights painted through the Custom Highlight API are not in the
 * DOM, so they cannot be clicked — there is no element to attach a
 * handler to. Hit-testing the ranges directly gets the behaviour back
 * without giving up the reason for using the API: the document's own
 * markup is never rewritten to carry a mark.
 */
export function threadAtPoint(
  ranges: Map<string, Range>,
  x: number,
  y: number,
): string | null {
  const caret = caretAt(x, y);
  if (!caret) return null;
  for (const [key, range] of ranges) {
    try {
      if (range.isPointInRange(caret.node, caret.offset)) return key;
    } catch {
      // A range whose nodes have been replaced under it — the document
      // was revised while the thread stayed. Not a match, not a crash.
    }
  }
  return null;
}

/** The text position under a point, across the two browser spellings. */
function caretAt(x: number, y: number): { node: Node; offset: number } | null {
  const legacy = (
    document as unknown as { caretRangeFromPoint?: (x: number, y: number) => Range | null }
  ).caretRangeFromPoint;
  if (typeof legacy === "function") {
    const range = legacy.call(document, x, y);
    return range ? { node: range.startContainer, offset: range.startOffset } : null;
  }
  const standard = (
    document as unknown as {
      caretPositionFromPoint?: (
        x: number,
        y: number,
      ) => { offsetNode: Node; offset: number } | null;
    }
  ).caretPositionFromPoint;
  if (typeof standard === "function") {
    const position = standard.call(document, x, y);
    return position ? { node: position.offsetNode, offset: position.offset } : null;
  }
  return null;
}

/**
 * Mark every commented passage, and light the one in focus.
 *
 * An anchor that appears only while its card is hovered is an anchor
 * nobody knows is there — which is most of why the highlighting
 * looked as though it was not working. These are drawn for as long as
 * the thread exists.
 *
 * The ranges are handed back out because a highlight painted through
 * the Custom Highlight API is not in the DOM and cannot be clicked;
 * hit-testing them is what makes the passage lead back to its thread.
 *
 * Recomputed when the version changes, because answering a comment
 * usually rewrites the passage it quoted and the old range then
 * points at text that is no longer there.
 */
function useMarks(
  bodyRef: React.RefObject<HTMLElement | null>,
  threads: Thread[],
  focused: string | null,
  version: number,
  out: React.RefObject<Map<string, Range>>,
) {
  useEffect(() => {
    const body = bodyRef.current;
    const found = new Map<string, Range>();
    if (body) {
      for (const thread of threads) {
        if (!thread.quote.trim()) continue;
        const range = findText(body, thread.quote, thread.quoteStart);
        if (range) found.set(thread.key, range);
      }
    }
    out.current = found;

    const highlights =
      typeof CSS === "undefined"
        ? undefined
        : (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
    const HighlightCtor = (
      window as unknown as { Highlight?: new (...r: Range[]) => unknown }
    ).Highlight;
    if (!highlights || !HighlightCtor) return;

    const marks = [...found.entries()].filter(([key]) => key !== focused).map(([, r]) => r);
    const lit = focused ? found.get(focused) : undefined;

    if (marks.length) highlights.set("doc-comment-mark", new HighlightCtor(...marks));
    else highlights.delete("doc-comment-mark");
    if (lit) highlights.set("doc-comment-quote", new HighlightCtor(lit));
    else highlights.delete("doc-comment-quote");

    return () => {
      highlights.delete("doc-comment-mark");
      highlights.delete("doc-comment-quote");
    };
    // ``version`` is in here on purpose: the ranges are stale the
    // moment the document is rewritten under them.
  }, [bodyRef, threads, focused, version, out]);
}

/** Stable, so putting the rail away does not re-run the mark effect twice. */
const NO_THREADS: Thread[] = [];

export function DocumentComment({
  client,
  artifactId,
  n,
  bodyRef,
  onRevised,
  hidden = false,
  onHiddenChange,
}: {
  client: IgniClient;
  artifactId: string;
  n: number;
  /** The element holding the document, so only its text can be quoted. */
  bodyRef: React.RefObject<HTMLElement | null>;
  /** The agent finished; the document may have a new version. */
  onRevised?: () => void;
  /**
   * Put away, so the document has the page to itself.
   *
   * Held above this component, not in it: the width of the rail is the
   * width the document does not get, so the split has to know. Nothing
   * is unmounted — a running reply keeps running and every thread is
   * where it was on the way back.
   */
  hidden?: boolean;
  onHiddenChange?: (hidden: boolean) => void;
}) {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [menu, setMenu] = useState<Anchor | null>(null);
  /** The thread the reader is on: lit in the rail and in the document. */
  const [focused, setFocused] = useState<string | null>(null);
  const seq = useRef(0);
  /** Where each thread's passage currently sits, for hit-testing. */
  const ranges = useRef(new Map<string, Range>());
  /** The passage the context menu was opened on. A ref, not state: it
   *  is written while the selection still exists and read after focus
   *  has moved and collapsed it. */
  const pending = useRef({ quote: "", start: -1 });

  // Hidden clears the highlights too. A marked passage is a link to a
  // thread, and leaving the links painted while the thing they lead to
  // is put away gives the reader underlined sentences that do nothing
  // — which is the opposite of what hiding the comments was for.
  useMarks(bodyRef, hidden ? NO_THREADS : threads, focused, n, ranges);

  /** Put the document's view on a thread's passage. */
  const revealPassage = useCallback((key: string) => {
    const range = ranges.current.get(key);
    const target = range?.startContainer.parentElement;
    if (typeof target?.scrollIntoView === "function") {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, []);

  /** Put the rail's view on a thread. */
  const revealThread = useCallback((key: string) => {
    const card = document.querySelector(`[data-thread="${key}"]`);
    if (typeof (card as HTMLElement)?.scrollIntoView === "function") {
      (card as HTMLElement).scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, []);

  const send = useCallback(
    async (threadKey: string, body: string) => {
      const text = body.trim();
      if (!text) return;
      const thread = threads.find((t) => t.key === threadKey);
      if (!thread) return;

      const mine = `t${(seq.current += 1)}`;
      const answer = `t${(seq.current += 1)}`;
      const patchThread = (change: (t: Thread) => Thread) =>
        setThreads((prev) => prev.map((t) => (t.key === threadKey ? change(t) : t)));

      patchThread((t) => ({
        ...t,
        error: "",
        turns: [
          ...t.turns,
          { key: mine, who: "me", body: text, activity: "", phase: "done", error: "" },
          { key: answer, who: "agent", body: "", activity: "", phase: "running", error: "" },
        ],
      }));

      const patchTurn = (change: Partial<Turn>) =>
        patchThread((t) => ({
          ...t,
          turns: t.turns.map((turn) => (turn.key === answer ? { ...turn, ...change } : turn)),
        }));

      let posted: CommentPosted | null = null;
      try {
        posted = await client.rpc<CommentPosted>("artifact_comment", {
          id: artifactId,
          n,
          body: text,
          quote: thread.quote,
          quote_start: thread.quoteStart,
          quote_end: thread.quoteStart >= 0 ? thread.quoteStart + thread.quote.length : -1,
          // The thread's own id once it has one. Everything written
          // into a thread joins it; only the rail's "new comment"
          // affordance starts a fresh one.
          thread_id: thread.id,
        });
      } catch (e) {
        patchThread((t) => ({
          ...t,
          error: e instanceof Error ? e.message : String(e),
          turns: t.turns.filter((turn) => turn.key !== mine && turn.key !== answer),
        }));
        return;
      }

      if (!posted?.ok) {
        patchThread((t) => ({
          ...t,
          error: posted?.message || "Could not record that comment.",
          turns: t.turns.filter((turn) => turn.key !== mine && turn.key !== answer),
        }));
        return;
      }

      const threadId = posted.thread_id || thread.id;
      patchThread((t) => ({ ...t, id: threadId }));

      let reply = "";
      client
        .runMessage(posted.prompt, (m) => {
          if (m.type === "content_delta" && !m.is_thinking) {
            reply += m.text;
            patchTurn({ body: reply });
          } else if (m.type === "tool_started") {
            patchTurn({ activity: m.friendly_name || m.tool_name });
          } else if (m.type === "run_started" && m.parent_run_id) {
            // A delegated run — the archivist, usually. Naming it is
            // the difference between "something is happening" and
            // knowing who is doing it.
            patchTurn({ activity: `${m.agent_name} is working` });
          } else if (m.type === "run_error") {
            patchTurn({ phase: "failed", error: m.error });
          }
        })
        .then(() => {
          patchTurn({ phase: "done", activity: "" });
          // Record what the agent said, on the same thread, so the
          // transcript lives with the document rather than in a chat
          // session that ends when the process does.
          if (reply.trim()) {
            void client
              .rpc("artifact_comment", {
                id: artifactId,
                n,
                body: reply,
                thread_id: threadId,
                origin: "agent",
              })
              .catch(() => {
                // A transcript that failed to record is not worth
                // interrupting the reader over; the reply is on
                // screen either way.
              });
          }
          // The answer to a comment is usually a new version, so the
          // page under it is now out of date with itself.
          onRevised?.();
        })
        .catch((e) => {
          patchTurn({
            phase: "failed",
            activity: "",
            error: e instanceof Error ? e.message : String(e),
          });
        });
    },
    [client, artifactId, n, threads, onRevised],
  );

  /** Start a thread, on a passage or on the document. */
  const startThread = useCallback(
    (quote: string, quoteStart = -1) => {
      const key = `th${(seq.current += 1)}`;
      setThreads((prev) => [
        ...prev,
        { key, id: "", quote, quoteStart, turns: [], error: "" },
      ]);
      setFocused(key);
      setMenu(null);
      // Asking a question is asking for the rail back. Right-click →
      // Comment still works with it put away, and having the new
      // thread land somewhere invisible would read as nothing
      // happening.
      onHiddenChange?.(false);
      return key;
    },
    [onHiddenChange],
  );

  // ── Right-click a selection ─────────────────────────────────────
  useEffect(() => {
    const onContextMenu = (e: MouseEvent) => {
      const body = bodyRef.current;
      if (!body) return;
      const target = e.target as Node | null;
      if (!target || !body.contains(target)) return;

      const picked = window.getSelection();
      if (!picked || picked.isCollapsed) return;
      // Only text inside the document counts. Without this, selecting
      // a version number in the list below would be quoted as though
      // it were part of the document.
      if (!body.contains(picked.anchorNode)) return;
      const text = picked.toString().trim();
      if (!text) return;

      e.preventDefault();

      const range = picked.getRangeAt(0);
      pending.current = {
        quote: text.slice(0, 10_000),
        start: offsetWithin(body, range.startContainer, range.startOffset),
      };

      // Keyboard-invoked menus report no useful pointer, so fall back
      // to the selection's own box — which is arguably the better
      // anchor anyway, being attached to the words in question.
      let x = e.clientX;
      let y = e.clientY;
      if (!x && !y) {
        const rect = range.getBoundingClientRect();
        x = rect.left;
        y = rect.bottom;
      }
      setMenu({
        x: Math.min(x, window.innerWidth - 200 - EDGE_MARGIN),
        y: Math.min(y, window.innerHeight - 60 - EDGE_MARGIN),
      });
    };

    document.addEventListener("contextmenu", onContextMenu);
    return () => document.removeEventListener("contextmenu", onContextMenu);
  }, [bodyRef]);

  // ── Click a marked passage, get its thread ──────────────────────
  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const onClick = (e: MouseEvent) => {
      // Never over a selection: the reader is picking text, and
      // stealing that click would make selecting a marked sentence
      // impossible.
      if (!window.getSelection()?.isCollapsed) return;
      const key = threadAtPoint(ranges.current, e.clientX, e.clientY);
      if (!key) return;
      setFocused(key);
      revealThread(key);
    };
    body.addEventListener("click", onClick);
    return () => body.removeEventListener("click", onClick);
  }, [bodyRef, revealThread, threads]);

  // Esc closes the menu. Capture phase, so the page behind does not
  // also navigate back on the same keypress.
  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setMenu(null);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [menu]);

  const started = useMemo(
    () => threads.filter((t) => t.turns.length > 0 || t.key === focused),
    [threads, focused],
  );

  /* Shared by both states. Right-click → Comment is the main way in,
     and it goes on working with the rail put away — the thread it
     starts brings the rail back with it. Leaving this inside the open
     branch meant a reader who had hidden the comments could not start
     one without first finding the menu entry that unhides them. */
  const selectionMenu = menu ? (
    <>
      <div className="doc-selection-backdrop" onClick={() => setMenu(null)} />
      <div className="doc-selection-pop" style={{ left: menu.x, top: menu.y }} role="menu">
        <button
          type="button"
          role="menuitem"
          className="popup-item overflow-item"
          autoFocus
          onClick={() => {
            const key = startThread(pending.current.quote, pending.current.start);
            window.requestAnimationFrame(() => revealThread(key));
          }}
        >
          Comment on selection
        </button>
      </div>
    </>
  ) : null;

  /* Put away, the rail is an icon rather than nothing. The way back
     has to be where the thing went — a control that lives only in a
     menu asks the reader to remember that comments exist at all — but
     it should cost the page as little as the thing it replaced. A
     speech bubble says "comments" in the width of a bubble; the
     word set sideways down a 30px strip said the same thing in a
     column of rotated capitals nobody reads. The count rides on it,
     which is the part actually worth keeping on screen. */
  if (hidden) {
    return (
      <aside className="doc-rail doc-rail-stowed" aria-label="Comments">
        <button
          type="button"
          className="doc-rail-restore"
          onClick={() => onHiddenChange?.(false)}
          aria-expanded={false}
          // Named, because an icon has no text to be named by.
          aria-label={started.length ? `Show comments (${started.length})` : "Show comments"}
          title={started.length ? `Show comments (${started.length})` : "Show comments"}
        >
          <CommentIcon />
          {started.length > 0 && (
            <span className="doc-rail-stowed-count">{started.length}</span>
          )}
        </button>
        {selectionMenu}
      </aside>
    );
  }

  return (
    <aside className="doc-rail" aria-label="Comments">
      <header className="doc-rail-head">
        <h3 className="doc-rail-title">Comments</h3>
        {started.length > 0 && <span className="doc-rail-count">{started.length}</span>}
        <button
          type="button"
          className="doc-rail-hide"
          onClick={() => onHiddenChange?.(true)}
          aria-expanded
          title="Hide comments"
        >
          <Chevron />
        </button>
      </header>

      <div className="doc-rail-body">
        {started.length === 0 ? (
          <p className="doc-rail-empty">
            Nothing yet. Select a passage and right-click to ask about it, or comment on the
            document as a whole.
          </p>
        ) : (
          <ol className="doc-conv-list">
            {started.map((thread) => (
              <ThreadView
                key={thread.key}
                thread={thread}
                bodyRef={bodyRef}
                focused={focused === thread.key}
                onFocus={() => {
                  setFocused(thread.key);
                  revealPassage(thread.key);
                }}
                onSend={(body) => void send(thread.key, body)}
              />
            ))}
          </ol>
        )}

        <div className="doc-comment-bar">
          <button type="button" className="btn btn-sm" onClick={() => startThread("")}>
            New comment
          </button>
          {/* Names the other way in, because a right-click affordance
              is not discoverable by looking. */}
          <span className="doc-comment-hint">For one passage, select it and right-click</span>
        </div>
      </div>

      {selectionMenu}
    </aside>
  );
}

/** One conversation, with its own composer at the foot. */
function ThreadView({
  thread,
  bodyRef,
  focused,
  onFocus,
  onSend,
}: {
  thread: Thread;
  bodyRef: React.RefObject<HTMLElement | null>;
  focused: boolean;
  onFocus: () => void;
  onSend: (body: string) => void;
}) {
  // Checked on render rather than remembered: answering a comment
  // usually rewrites the very passage it was about, so the common
  // case is that this becomes false a moment after the reply lands.
  const anchored = quoteStillPresent(bodyRef.current, thread.quote);
  const running = isRunning(thread);

  return (
    <li
      className={focused ? "doc-conv-item doc-conv-focused" : "doc-conv-item"}
      data-thread={thread.key}
    >
      {/* The whole card is the way back to the passage — a link on
          the quote alone would be a small target next to a large
          inert one. */}
      <button type="button" className="doc-conv-anchor" onClick={onFocus}>
        {thread.quote ? (
          <>
            <span className="doc-conv-quote">{thread.quote}</span>
            {/* Said, not left to be noticed. A quote that no longer
                appears in the document simply stops lighting up, and
                a highlight that does nothing is indistinguishable
                from one that is broken. */}
            {!anchored && <span className="doc-conv-moved">This passage has changed since</span>}
          </>
        ) : (
          <span className="doc-conv-scope">About the whole document</span>
        )}
      </button>

      {thread.turns.map((turn) => (
        <TurnView key={turn.key} turn={turn} />
      ))}

      {thread.error && <div className="msg-error">{thread.error}</div>}

      {/* Part of the thread, not a button that opens one elsewhere.
          Continuing is the default; starting a new conversation is
          the deliberate act. */}
      <ThreadComposer
        busy={running}
        first={thread.turns.length === 0}
        onSend={onSend}
      />
    </li>
  );
}

/** One turn — mine, or the answer to it. */
function TurnView({ turn }: { turn: Turn }) {
  if (turn.who === "me") {
    return <p className="doc-conv-body">{turn.body}</p>;
  }

  return (
    <div className="doc-conv-reply" aria-live="polite">
      {turn.phase === "failed" ? (
        <div className="msg-error">{turn.error || "That run did not finish."}</div>
      ) : turn.body ? (
        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.body}</ReactMarkdown>
        </div>
      ) : (
        /* Something rather than a blank space. A comment that appears
           to do nothing for twenty seconds reads as a comment that
           was dropped. */
        <p className="doc-conv-waiting">{turn.activity || "Thinking…"}</p>
      )}

      {turn.phase === "running" && turn.body && (
        <span className="doc-conv-cursor" aria-hidden="true" />
      )}
    </div>
  );
}

/** The small input at the foot of a thread. */
function ThreadComposer({
  busy,
  first,
  onSend,
}: {
  busy: boolean;
  /** Nothing has been said yet, so this is the opening remark. */
  first: boolean;
  onSend: (body: string) => void;
}) {
  const [body, setBody] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (first) input.current?.focus();
  }, [first]);

  const submit = () => {
    if (!body.trim() || busy) return;
    onSend(body);
    setBody("");
  };

  return (
    <div className="doc-thread-compose">
      <textarea
        ref={input}
        className="doc-comment-input"
        rows={first ? 2 : 1}
        value={body}
        disabled={busy}
        placeholder={first ? "What should change, or what do you want to know?" : "Reply…"}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          // Enter sends, because this is a message. Shift+Enter for a
          // newline, the same bargain the chat composer makes.
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button
        type="button"
        className="btn btn-sm btn-primary doc-thread-send"
        disabled={busy || !body.trim()}
        onClick={submit}
      >
        {busy ? "…" : "Ask"}
      </button>
    </div>
  );
}

/**
 * The arrow on the hide control.
 *
 * Points the way the rail travels — out towards the edge it sits on.
 * Coming back is the icon's job, and an icon that is recognisably a
 * comment needs no arrow to explain what it restores.
 */
function Chevron() {
  return (
    <svg
      className="doc-rail-chevron"
      viewBox="0 0 16 16"
      width="13"
      height="13"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M6 3.5 10.5 8 6 12.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * A speech bubble, for the rail once it is put away.
 *
 * The one place in this component where a picture beats a word: it
 * has to read at 16px, in a lane too narrow for "Comments" to be set
 * across.
 */
function CommentIcon() {
  return (
    <svg
      className="doc-rail-stowed-icon"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M13.5 2.5h-11v8h3v2.5l3-2.5h5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}
