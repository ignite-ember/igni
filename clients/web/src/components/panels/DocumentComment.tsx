import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { IgniClient } from "../../protocol/client";
import { formatExact, formatRelative } from "../../utils/when";

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
  /** Who wrote it, for turns read back out of the store. Absent on
   *  turns written here, where "me" is the whole answer. */
  author?: string;
  /** When it was written. Absent for the same reason. */
  at?: string;
}

/** One stored comment, as the rail reads it back. */
export interface StoredComment {
  comment_id: string;
  thread_id: string;
  parent_id: string;
  origin: string;
  quote: string;
  /** Where the passage sat when it was written, or -1. */
  quote_start?: number;
  body: string;
  status: string;
  author: string;
  at: string;
  n: number;
}

/**
 * Stored comments, as threads.
 *
 * Grouped by ``thread_id`` with the opening comment first, because a
 * conversation read back as a flat list in timestamp order puts every
 * answer next to the wrong question. Threads are ordered by their
 * opening comment, newest first: the argument people are having now
 * is the one worth being at the top.
 *
 * Exported for its own test — the grouping is the part that is wrong
 * in ways a screenshot does not show.
 */
export function threadsFrom(rows: StoredComment[]): Thread[] {
  const byThread = new Map<string, StoredComment[]>();
  for (const row of rows) {
    // A comment with no thread id is its own thread. Falling back to
    // the comment id keeps it separate rather than collapsing every
    // such comment into one.
    const key = row.thread_id || row.comment_id;
    const group = byThread.get(key);
    if (group) group.push(row);
    else byThread.set(key, [row]);
  }

  const threads: Thread[] = [];
  for (const [key, group] of byThread) {
    const roots = group.filter((r) => !r.parent_id);
    const replies = group.filter((r) => r.parent_id);
    const ordered = [
      ...roots.sort((a, b) => a.at.localeCompare(b.at)),
      ...replies.sort((a, b) => a.at.localeCompare(b.at)),
    ];
    if (!ordered.length) continue;
    threads.push({
      key: `stored-${key}`,
      // The *thread's* id, which is what a reply has to be posted
      // against. The opening comment's own id is a different string —
      // the backend mints ``local-thread-…`` for a new thread — so
      // sending that back joined the reply to nothing.
      id: ordered[0].thread_id || ordered[0].comment_id,
      quote: ordered[0].quote,
      // Carried through, because it is the only thing that tells two
      // identical sentences apart — without it a comment on the
      // second one lands on the first.
      quoteStart: ordered[0].quote_start ?? -1,
      status: ordered[0].status,
      n: ordered[0].n,
      turns: ordered.map((r) => ({
        key: r.comment_id,
        who: r.origin === "agent" ? "agent" : "me",
        body: r.body,
        activity: "",
        phase: "done" as const,
        error: "",
        author: r.author,
        at: r.at,
      })),
      error: "",
    });
  }
  return threads.sort((a, b) =>
    (b.turns[0]?.at ?? "").localeCompare(a.turns[0]?.at ?? ""),
  );
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
  /** ``open``, ``accepted``, ``rejected``… for threads read back out
   *  of the store. Absent on threads started here, which are open by
   *  definition. */
  status?: string;
  /** The version the thread was left against. */
  n?: number;
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

/**
 * Where a quote sits in the document, as a position in its text.
 *
 * The one place that answers "where is this passage", so the rail's
 * order and the highlight can never disagree about it. Sorting the
 * rail by a plainer ``indexOf`` looked right and was not: the
 * document's text and a stored quote differ in their whitespace the
 * moment the passage wraps, so quotes that highlighted perfectly well
 * failed to be *found* and fell to the bottom of the list.
 *
 * Whitespace is squashed on both sides for the same reason, and
 * ``near`` breaks the tie when a sentence appears more than once —
 * the comment is on one of them, not on the first.
 *
 * Returns -1 for a passage that is not there any more.
 */
export function whereIn(flat: string, needle: string, near = -1): number {
  if (!needle.trim()) return -1;
  const squash = (s: string) => s.replace(/\s+/g, " ");
  const haystack = squash(flat);
  const target = squash(needle);

  const hits: number[] = [];
  for (let i = haystack.indexOf(target); i >= 0; i = haystack.indexOf(target, i + 1)) {
    hits.push(i);
  }
  if (!hits.length) return -1;

  // Nearest to where the passage was when the comment was written.
  // With no hint, or after an edit moved everything, the first is as
  // good an answer as any — and is at least stable.
  return near < 0
    ? hits[0]
    : hits.reduce((best, i) => (Math.abs(i - near) < Math.abs(best - near) ? i : best), hits[0]);
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

  const target = needle.replace(/\s+/g, " ");
  const at = whereIn(flat, needle, near);
  if (at < 0) return null;

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

    // Nothing selected: every commented passage is marked, which is
    // how a reader finds out the document has been talked about.
    // Something selected: only that one. Leaving the rest lit while
    // one is being read turns the answer to "which passage is this
    // card about" back into the question — everything is blue again.
    const lit = focused ? found.get(focused) : undefined;
    const marks = focused ? [] : [...found.values()];

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

/** Everything this component puts on the page, in one selector. */
const MINE =
  ".doc-rail, .doc-rail-away, .doc-selection-pop, .doc-selection-backdrop, .doc-filter-pop";

/**
 * What the rail is being narrowed to.
 *
 * One list per question, and the usual bargain for a faceted filter:
 * *any* of the values inside a list, *all* of the lists. Ticking both
 * "Unresolved" and "Resolved" therefore says the same as ticking
 * neither, which is the behaviour people expect and the reason a set
 * is the right shape here rather than a flag — a flag has no way to
 * say "either".
 *
 * An empty list restricts nothing, so "no filter" is always
 * reachable by clearing.
 */
export interface CommentFilter {
  /** ``unresolved`` — still open. ``resolved`` — accepted or declined. */
  status?: ("unresolved" | "resolved")[];
  /** Whether igni has replied in the thread. */
  answer?: ("answered" | "unanswered")[];
  /** Whether the quoted passage is still in the document. */
  passage?: ("here" | "gone")[];
  /** The versions a thread was left against. */
  versions?: number[];
}

/** Every facet, in the order they are offered. */
const FACETS = ["status", "answer", "passage", "versions"] as const;

export function filterIsEmpty(f: CommentFilter): boolean {
  return FACETS.every((k) => !f[k]?.length);
}

/** A thread that has been accepted or declined is settled. */
function isSettled(thread: Thread): boolean {
  return !!thread.status && thread.status !== "open";
}

/**
 * Whether a thread survives the filter.
 *
 * Kept out of the component for its own test: a filter that hides the
 * wrong thing is invisible — the rail just looks emptier, and the
 * reader concludes the comments are gone rather than hidden.
 */
export function keeps(f: CommentFilter, thread: Thread, anchored: boolean): boolean {
  const within = <T,>(chosen: T[] | undefined, actual: T) =>
    !chosen?.length || chosen.includes(actual);

  return (
    within(f.status, isSettled(thread) ? "resolved" : "unresolved") &&
    within(f.answer, thread.turns.some((t) => t.who === "agent") ? "answered" : "unanswered") &&
    within(f.passage, anchored ? "here" : "gone") &&
    (!f.versions?.length || f.versions.includes(thread.n ?? 0))
  );
}

/** One thing the reader has chosen, and how to take it back off. */
export interface FilterChip {
  key: string;
  label: string;
  drop: (f: CommentFilter) => CommentFilter;
}

const FACET_LABELS: Record<string, string> = {
  unresolved: "Unresolved",
  resolved: "Resolved",
  answered: "Answered by igni",
  unanswered: "Not answered",
  here: "Passage still there",
  gone: "Passage rewritten",
};

/**
 * The filter as a row of chips.
 *
 * Every restriction on screen, each removable on its own. Behind a
 * button, the only sign that anything was hidden is a number, and
 * the reader has to open a menu to find out *what* they asked for —
 * which is the moment they stop trusting the count.
 */
export function chipsOf(f: CommentFilter): FilterChip[] {
  const out: FilterChip[] = [];
  const pull = <K extends "status" | "answer" | "passage">(facet: K, value: string) => (
    prev: CommentFilter,
  ): CommentFilter => ({
    ...prev,
    [facet]: (prev[facet] as string[] | undefined)?.filter((v) => v !== value),
  });

  for (const facet of ["status", "answer", "passage"] as const) {
    for (const value of f[facet] ?? []) {
      out.push({
        key: `${facet}:${value}`,
        label: FACET_LABELS[value] ?? value,
        drop: pull(facet, value),
      });
    }
  }
  for (const n of f.versions ?? []) {
    out.push({
      key: `v:${n}`,
      label: `v${n}`,
      drop: (prev) => ({ ...prev, versions: prev.versions?.filter((v) => v !== n) }),
    });
  }
  return out;
}

/** A rect, or nothing at all, and never an exception. */
function measure(of: Range | Element): DOMRect | null {
  try {
    const get = (of as { getBoundingClientRect?: () => DOMRect }).getBoundingClientRect;
    return typeof get === "function" ? get.call(of) : null;
  } catch {
    return null;
  }
}

function ThreadLine({
  focused,
  ranges,
  version,
}: {
  focused: string | null;
  ranges: React.RefObject<Map<string, Range>>;
  /** The ranges are stale the moment the document is rewritten. */
  version: number;
}) {
  const [path, setPath] = useState("");

  useEffect(() => {
    if (!focused) {
      setPath("");
      return;
    }
    let frame = 0;

    const draw = () => {
      const range = ranges.current?.get(focused);
      const escape =
        typeof CSS !== "undefined" && typeof CSS.escape === "function"
          ? CSS.escape
          : (s: string) => s;
      const card = document.querySelector(`[data-thread="${escape(focused)}"]`);
      if (!range || !card) {
        setPath("");
        return;
      }
      // Measured defensively. A Range whose nodes have been removed
      // throws, and the environment need not implement the method at
      // all — this line is decoration, and decoration that can take
      // the conversation down with it is worse than no line.
      const from = measure(range);
      const whole = measure(card);
      if (!from || !whole) {
        setPath("");
        return;
      }

      // As much of the card as can actually be seen.
      //
      // Two things go wrong without this. A thread with a long answer
      // in it is taller than the window, so an outline around the
      // whole card is a rectangle drawn across the page rather than a
      // mark on the thing selected. And the rail scrolls inside
      // itself, so an unclamped outline runs up over the version
      // history sitting above it.
      const rail = card.closest(".doc-rail-body");
      const within = (rail && measure(rail)) || null;
      const top = Math.max(whole.top, within?.top ?? 0, 0);
      const bottom = Math.min(
        whole.bottom,
        within?.bottom ?? window.innerHeight,
        window.innerHeight,
      );
      if (bottom - top < 12) {
        setPath("");
        return;
      }
      const to = new DOMRect(whole.left, top, whole.width, bottom - top);
      // Both ends have to be on screen. Either one scrolled out of
      // the window — the passage above the fold, or the card out of
      // the rail's own scroll — draws a line that leaves the page and
      // points at nothing, which reads as a rendering fault rather
      // than as an answer about where the other end is.
      const onScreen = (r: DOMRect) =>
        (r.width || r.height) && r.bottom > 0 && r.top < window.innerHeight;
      if (!onScreen(from) || !onScreen(to)) {
        setPath("");
        return;
      }

      const x1 = from.right + 6;
      const y1 = from.top + from.height / 2;
      const x2 = to.left - 6;
      // Near the top of what is visible, not the middle of it: the
      // middle of a tall card is the middle of somebody's answer.
      const y2 = to.top + Math.min(20, to.height / 2);
      // Horizontal control points, so the curve leaves the sentence
      // sideways and arrives at the card sideways. Pulled out by the
      // gap itself, so a short hop stays gentle and a long one does
      // not bow into the text.
      const pull = Math.max(24, Math.min(140, (x2 - x1) / 2));

      setPath(`M${x1},${y1} C${x1 + pull},${y1} ${x2 - pull},${y2} ${x2},${y2}`);
    };

    draw();
    // Both boxes move independently, so the line is re-measured
    // rather than positioned once. Capture, because the scroll that
    // matters is a container's and does not bubble.
    const follow = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    };
    window.addEventListener("scroll", follow, true);
    window.addEventListener("resize", follow);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
    };
  }, [focused, ranges, version]);

  if (!path) return null;
  return createPortal(
    <svg className="doc-thread-line" aria-hidden="true">
      <path className="doc-thread-curve" d={path} />
    </svg>,
    document.body,
  );
}

/**
 * Whether a message is addressed to igni.
 *
 * The one thing that decides whether a model runs. Everything in this
 * rail used to run one: the rail was local, the agent was the only
 * other party, and "reply" and "ask" were the same act. They stopped
 * being the same act when the rail started showing threads other
 * people are in — a remark to a colleague is not an instruction, and
 * an agent that took every remark as one would set about editing the
 * document each time somebody agreed with somebody else.
 *
 * Only an explicit mention counts. Inferring it from tone would put
 * the question to the one person who never needed to think about it.
 */
export function mentionsIgni(text: string): boolean {
  return /(?:^|[\s(])@igni\b/i.test(text);
}

export function DocumentComment({
  client,
  artifactId,
  n,
  bodyRef,
  onRevised,
  frozen = false,
  hidden = false,
  switched = false,
  onThreadStarted,
  onHiddenChange,
}: {
  client: IgniClient;
  artifactId: string;
  n: number;
  /**
   * The document is archived and cannot take a new version.
   *
   * Said here rather than left to the write to refuse. Asking igni to
   * change a passage, watching it work, and being told at the end
   * that the document is frozen is a minute spent on an answer that
   * was never going to land.
   */
  frozen?: boolean;
  /**
   * Something above this owns the showing and hiding.
   *
   * The rail grew its own chevron when it was a panel beside the
   * text. In the column it is one of two sections under a switch, so
   * it keeps neither the chevron nor the put-away strip — two ways to
   * do one thing, one of which is invisible from where the other
   * lives.
   */
  switched?: boolean;
  /** A thread was started while the conversation was hidden. */
  onThreadStarted?: () => void;
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
  /** The conversation already on this document, read back on open. */
  const [stored, setStored] = useState<Thread[]>([]);
  /** What the rail is narrowed to, and whether the chooser is open. */
  const [filter, setFilter] = useState<CommentFilter>({});
  const [filtering, setFiltering] = useState(false);
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
  /**
   * Every thread on screen, newest first.
   *
   * Defined here rather than beside the list that renders it because
   * the highlights are computed from it: fed only the threads started
   * in this session, the passages behind a document's *existing*
   * comments never got a range, so clicking one of those cards lit
   * nothing up and read as a dead card.
   */
  const started = useMemo(
    () => [
      // What is being written now, above what was settled before it.
      ...threads.filter((t) => t.turns.length > 0 || t.key === focused),
      ...stored,
    ],
    [threads, stored, focused],
  );

  /**
   * The threads in the order their passages appear in the document.
   *
   * Not the order they were written. A margin conversation is read
   * alongside the text, so a comment on the first paragraph belongs
   * at the top of the rail however recently it was made — sorted by
   * recency, the card beside a sentence is nowhere near it, and the
   * reader has to search a list to find the one they are looking at.
   *
   * Three groups. The anchored ones first, in the document's own
   * order. Then the ones whose passage has since been rewritten: they
   * cannot be placed, and guessing a position for them would put them
   * somewhere they do not belong. Then the ones about the whole
   * document, which sit under all of it for the same reason a note on
   * an article goes at its foot rather than across its title — a
   * remark about the whole thing is made once the whole thing has
   * been read. Ties keep the order they arrived in, which is newest
   * first.
   */
  const ordered = useMemo(() => {
    const text = bodyRef.current?.textContent ?? "";
    // Past the end of any real position, so nothing in the document
    // can sort below them, and ordered among themselves so a lost
    // passage still reads before a note on the whole document.
    const PASSAGE_GONE = Number.MAX_SAFE_INTEGER - 1;
    const WHOLE_DOCUMENT = Number.MAX_SAFE_INTEGER;
    const place = (thread: Thread) => {
      if (!thread.quote.trim()) return WHOLE_DOCUMENT;
      // The same lookup the highlight uses, so a passage the rail
      // places and a passage the document lights up are the same
      // passage. A plainer search disagreed with it on any quote
      // whose whitespace had moved.
      const at = whereIn(text, thread.quote, thread.quoteStart);
      return at >= 0 ? at : PASSAGE_GONE;
    };
    return [...started]
      .map((thread) => ({ thread, at: place(thread) }))
      .sort((a, b) => a.at - b.at);
  }, [started, bodyRef, n]);

  /**
   * The threads grouped by the passage they are about.
   *
   * The same sentence commented on twice — often on two versions, a
   * month apart — was two cards quoting the same words back. The
   * repetition reads as a rendering fault long before it reads as
   * two conversations, and the thing they have in common, the
   * passage, is stated twice while the thing that differs, the
   * version, is buried in each.
   *
   * Grouped on *position as well as text*, so a heading that appears
   * twice in a document does not collect the comments from both
   * places into one pile.
   */
  const groups = useMemo(() => {
    const out: { key: string; at: number; quote: string; threads: Thread[] }[] = [];
    for (const { thread, at } of ordered) {
      const last = out[out.length - 1];
      const same =
        last &&
        last.at === at &&
        last.quote.replace(/\s+/g, " ") === thread.quote.replace(/\s+/g, " ");
      if (same) last.threads.push(thread);
      else out.push({ key: thread.key, at, quote: thread.quote, threads: [thread] });
    }
    // Oldest first inside a group. The conversations about one
    // sentence are its history, and a history reads forwards.
    for (const g of out) {
      g.threads.sort((a, b) => (a.n ?? 0) - (b.n ?? 0));
    }
    return out;
  }, [ordered]);

  /**
   * The groups the filter leaves.
   *
   * Applied to the conversations and not to the passages, so a
   * passage with four remarks on it and one of them open does not
   * vanish from a rail narrowed to open ones — it shows the one.
   * A group that loses all of them goes with them: a quote with no
   * conversation under it is a heading for nothing.
   */
  const shown = useMemo(() => {
    if (filterIsEmpty(filter)) return groups;
    const body = bodyRef.current;
    return groups
      .map((g) => ({
        ...g,
        threads: g.threads.filter((th) =>
          keeps(filter, th, quoteStillPresent(body, g.quote)),
        ),
      }))
      .filter((g) => g.threads.length > 0);
  }, [groups, filter, bodyRef]);

  /** Every version this document has been commented on. */
  const commentedVersions = useMemo(() => {
    const seen = new Set<number>();
    for (const g of groups) for (const th of g.threads) if (th.n) seen.add(th.n);
    return [...seen].sort((a, b) => b - a);
  }, [groups]);

  const chips = useMemo(() => chipsOf(filter), [filter]);

  const hiddenCount = useMemo(
    () =>
      groups.reduce((n, g) => n + g.threads.length, 0) -
      shown.reduce((n, g) => n + g.threads.length, 0),
    [groups, shown],
  );

  const orderedThreads = useMemo(() => ordered.map((o) => o.thread), [ordered]);

  useMarks(bodyRef, hidden ? NO_THREADS : orderedThreads, focused, n, ranges);

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

  /**
   * Backing out of a composer.
   *
   * A thread that never got a turn goes with it. It was only on
   * screen because somebody pressed New comment, and leaving it
   * behind would put an empty card in the rail quoting a passage
   * nobody ended up saying anything about — indistinguishable from a
   * comment whose text failed to load.
   *
   * A thread that has been spoken in stays: cancelling a reply is
   * abandoning the reply, not the conversation.
   */
  const discard = useCallback((thread: Thread) => {
    if (thread.turns.length > 0) return;
    setThreads((prev) => prev.filter((t) => t.key !== thread.key));
    setFocused((key) => (key === thread.key ? null : key));
  }, []);

  const send = useCallback(
    async (threadKey: string, body: string) => {
      const text = body.trim();
      if (!text) return;
      // Every thread on screen, not only the ones started here. The
      // rail draws two lists — what this session opened and what the
      // document already had — and looking in the first one alone
      // meant replying to an existing comment found nothing and
      // returned: no post, no turn, no error. The reply vanished.
      const thread = started.find((t) => t.key === threadKey);
      if (!thread) return;

      const asksAgent = mentionsIgni(text);
      const mine = `t${(seq.current += 1)}`;
      const answer = `t${(seq.current += 1)}`;
      // Both lists, for the same reason. Keys are unique across them,
      // so whichever holds this thread is the one that changes.
      const patchThread = (change: (t: Thread) => Thread) => {
        setThreads((prev) => prev.map((t) => (t.key === threadKey ? change(t) : t)));
        setStored((prev) => prev.map((t) => (t.key === threadKey ? change(t) : t)));
      };

      patchThread((t) => ({
        ...t,
        error: "",
        turns: [
          ...t.turns,
          {
            key: mine,
            who: "me",
            body: text,
            activity: "",
            phase: "done",
            error: "",
            // Stamped here rather than waiting for the store to hand
            // it back: until a reload, a comment just written would
            // be the one turn in the thread with no date on it.
            at: new Date().toISOString(),
          },
          // The waiting turn only exists when something is coming. A
          // spinner under a remark to a colleague promises an answer
          // nobody is writing.
          ...(asksAgent
            ? [
                {
                  key: answer,
                  who: "agent" as const,
                  body: "",
                  activity: "",
                  phase: "running" as const,
                  error: "",
                  at: new Date().toISOString(),
                },
              ]
            : []),
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

      // Recorded, and that is all. A remark to the people in the
      // thread is not an instruction, and running a model on it would
      // answer a question nobody asked.
      if (!asksAgent) return;

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
    [client, artifactId, n, started, onRevised],
  );

  /** Start a thread, on a passage or on the document. */
  /**
   * The conversation this document already has.
   *
   * The rail used to start empty every time, holding only what was
   * said in this session — so a document with a hundred comments on
   * it looked like a document nobody had ever read. Loaded threads
   * sit under the ones started here, because what you are typing
   * belongs above what was settled last month.
   */
  useEffect(() => {
    let cancelled = false;
    void client
      .rpc<StoredComment[]>("artifact_comments", { id: artifactId, open_only: false })
      .then((rows) => {
        if (cancelled) return;
        setStored(threadsFrom(rows ?? []));
      })
      .catch(() => {
        // A conversation that failed to load is not worth replacing
        // the document with an error; the composer still works.
        if (!cancelled) setStored([]);
      });
    return () => {
      cancelled = true;
    };
  }, [client, artifactId]);

  /**
   * Going back to the page puts the selection down.
   *
   * The selection exists to answer "which passage is this card
   * about". Once the reader has looked away from the rail it has
   * been answered, and a wash and a curve left over the page is
   * furniture on top of the thing they were pointing at.
   *
   * Anywhere but the rail, rather than the prose alone. The margins,
   * the gutter and the space under a short document are most of what
   * is on screen, and a reader clicking off to the side of the text
   * has looked away just as plainly as one who clicked on it — a
   * dismissal that only works on the words is one people conclude
   * does not work.
   *
   * The rail is excluded rather than the document included, so
   * anything the page grows around it is covered by default. What
   * counts as the rail is named by what this component renders — an
   * ancestor it does not own would be a rule that holds only where
   * somebody remembered to wrap it.
   */
  useEffect(() => {
    const clear = (e: MouseEvent) => {
      const at = e.target as HTMLElement | null;
      if (at?.closest?.(MINE)) return;
      setFocused(null);
    };
    document.addEventListener("click", clear);
    return () => document.removeEventListener("click", clear);
  }, []);

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
      onThreadStarted?.();
      return key;
    },
    [onHiddenChange, onThreadStarted],
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
  if (hidden && switched) {
    // Mounted but not drawn: a reply in flight keeps running, every
    // thread is where it was on the way back, and a right-click on a
    // passage still opens one — the switch comes back on its own.
    return <div className="doc-rail-away">{selectionMenu}</div>;
  }

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

        {started.length > 0 && (
          <CommentFilters
            filter={filter}
            open={filtering}
            versions={commentedVersions}
            onOpen={setFiltering}
            onChange={setFilter}
          />
        )}
        {!switched && (
          <button
            type="button"
            className="doc-rail-hide"
            onClick={() => onHiddenChange?.(true)}
            aria-expanded
            title="Hide comments"
          >
            <Chevron />
          </button>
        )}
      </header>

      {frozen && (
        <p className="doc-rail-frozen">
          This document is archived. You can read the thread and add to it;
          nothing here can change the text until it is restored.
        </p>
      )}

      <div className="doc-rail-body">
        {/* What was asked for, on screen and each of it removable.
            Behind the button the only sign anything was hidden is a
            number, and finding out *what* was asked for costs a trip
            into a menu — which is the moment the reader stops
            trusting the number. */}
        {chips.length > 0 && (
          <div className="doc-filter-chips">
            {chips.map((chip) => (
              <button
                key={chip.key}
                type="button"
                className="doc-filter-chip"
                title={`Stop filtering by ${chip.label}`}
                onClick={() => setFilter(chip.drop)}
              >
                {chip.label}
                <span className="doc-filter-chip-x" aria-hidden="true">
                  ×
                </span>
              </button>
            ))}
            {chips.length > 1 && (
              <button
                type="button"
                className="doc-filter-clear"
                onClick={() => setFilter({})}
              >
                Clear all
              </button>
            )}
          </div>
        )}

        {/* Said, not left to be inferred from a short list. A filter
            that hides silently is indistinguishable from a document
            with fewer comments than the reader thought. */}
        {hiddenCount > 0 && (
          <p className="doc-filter-note">{hiddenCount} hidden.</p>
        )}

        {started.length === 0 ? (
          <p className="doc-rail-empty">
            Nothing yet. Select a passage and right-click to ask about it, or comment on the
            document as a whole.
          </p>
        ) : shown.length === 0 ? (
          <p className="doc-rail-empty">Nothing matches those filters.</p>
        ) : (
          <ol className="doc-conv-list">
            {shown.map((group) => (
              <PassageGroup
                key={group.key}
                group={group}
                bodyRef={bodyRef}
                focused={focused}
                onFocus={(thread) => {
                  // Pressing the selected one again puts it down. A
                  // selection with no way out but selecting something
                  // else leaves the document permanently marked up
                  // with an answer the reader has finished with.
                  //
                  // Except while it is being written. A thread with
                  // nothing said in it yet is only on screen because
                  // it is selected, so putting it down would take the
                  // box somebody is typing into with it.
                  const composing = thread.turns.length === 0;
                  const already = focused === thread.key && !composing;
                  setFocused(already ? null : thread.key);
                  if (!already) revealPassage(thread.key);
                }}
                onSend={(key, body) => void send(key, body)}
                onCancel={discard}
              />
            ))}
          </ol>
        )}
      </div>

      {/* Outside the scroller on purpose. As the last item in the list
          it sat below every comment on the document — a hundred of
          them on a busy one — so the way to start a new one was to
          scroll to the end of everyone else's. A footer is always
          where it was last time. */}
      <div className="doc-comment-bar">
        {/* Scrolled to, because a note on the whole document is filed
            under every comment on it — on a busy page that is past
            the bottom of the rail, and a button that files something
            out of sight reads as a button that did nothing. */}
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => {
            const key = startThread("");
            window.requestAnimationFrame(() => revealThread(key));
          }}
        >
          New comment
        </button>
        {/* Names the other way in, because a right-click affordance
            is not discoverable by looking. */}
        <span className="doc-comment-hint">For one passage, select it and right-click</span>
      </div>

      {selectionMenu}

      {/* The one line, from the passage being read to the card about
          it. Only while the rail is open: it joins two things and one
          of them is not on screen otherwise. */}
      <ThreadLine focused={focused} ranges={ranges} version={n} />
    </aside>
  );
}

/**
 * Narrowing the rail, by as many clauses as the reader wants.
 *
 * Grouped into the questions somebody actually asks — is it settled,
 * has igni answered, is the passage still there, which version — and
 * within a group the values are alternatives. That is what makes
 * "unresolved or resolved" mean the same as asking neither, and it
 * is why the groups need saying out loud: a flat list of eight
 * checkboxes gives no clue which of them compete.
 *
 * Behind a button, because most of the time nothing is filtered and
 * the control is a column of empty boxes taking the space the
 * conversation wants.
 */
function CommentFilters({
  filter,
  open,
  versions,
  onOpen,
  onChange,
}: {
  filter: CommentFilter;
  open: boolean;
  /** Every version this document has been commented on. */
  versions: number[];
  onOpen: (open: boolean) => void;
  onChange: (filter: CommentFilter) => void;
}) {
  const chips = chipsOf(filter);

  const toggle = (
    facet: "status" | "answer" | "passage",
    value: string,
  ) => {
    const was = (filter[facet] as string[] | undefined) ?? [];
    const next = was.includes(value) ? was.filter((v) => v !== value) : [...was, value];
    onChange({ ...filter, [facet]: next.length ? next : undefined });
  };

  const box = (
    facet: "status" | "answer" | "passage",
    value: string,
    label: string,
  ) => (
    <label key={value} className="doc-filter-row">
      <input
        type="checkbox"
        checked={((filter[facet] as string[] | undefined) ?? []).includes(value)}
        onChange={() => toggle(facet, value)}
      />
      {label}
    </label>
  );

  const section = (heading: string, children: React.ReactNode) => (
    <div className="doc-filter-section">
      <p className="doc-filter-heading">{heading}</p>
      {children}
    </div>
  );

  // Anchored to the button and drawn against the viewport. The rail
  // clips what scrolls inside it, and a menu positioned in there is
  // clipped with it — on a document with no comments the rail is two
  // lines tall and the chooser came out sawn off below the first
  // section.
  const anchor = useRef<HTMLButtonElement>(null);
  const [at, setAt] = useState<{ top: number; right: number } | null>(null);

  useEffect(() => {
    if (!open) {
      setAt(null);
      return;
    }
    const place = () => {
      const box = anchor.current?.getBoundingClientRect();
      if (!box) return;
      setAt({ top: box.bottom + 6, right: Math.max(8, window.innerWidth - box.right) });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  return (
    <div className="doc-filter">
      <button
        ref={anchor}
        type="button"
        className={chips.length ? "doc-filter-open is-on" : "doc-filter-open"}
        aria-expanded={open}
        title="Narrow what is shown"
        onClick={() => onOpen(!open)}
      >
        Filter
        {chips.length > 0 && <span className="doc-filter-count">{chips.length}</span>}
      </button>

      {open && at && createPortal(
        <>
          <div className="doc-filter-backdrop" onClick={() => onOpen(false)} />
          <div
            className="doc-filter-pop"
            role="group"
            aria-label="Filter comments"
            style={{ top: at.top, right: at.right }}
          >
            {section(
              "State",
              <>
                {box("status", "unresolved", "Unresolved")}
                {box("status", "resolved", "Resolved")}
              </>,
            )}
            {section(
              "Replies",
              <>
                {box("answer", "answered", "Answered by igni")}
                {box("answer", "unanswered", "Not answered")}
              </>,
            )}
            {section(
              "Passage",
              <>
                {box("passage", "here", "Still in the document")}
                {box("passage", "gone", "Rewritten since")}
              </>,
            )}
            {versions.length > 1 &&
              section(
                "Left on version",
                <div className="doc-filter-versions">
                  {versions.map((n) => (
                    <button
                      key={n}
                      type="button"
                      className={
                        filter.versions?.includes(n)
                          ? "doc-filter-version is-on"
                          : "doc-filter-version"
                      }
                      aria-pressed={filter.versions?.includes(n)}
                      onClick={() => {
                        const was = filter.versions ?? [];
                        const next = was.includes(n)
                          ? was.filter((v) => v !== n)
                          : [...was, n];
                        onChange({ ...filter, versions: next.length ? next : undefined });
                      }}
                    >
                      v{n}
                    </button>
                  ))}
                </div>,
              )}
          </div>
        </>,
        document.body,
      )}
    </div>
  );
}

/** One passage, and every conversation about it. */
function PassageGroup({
  group,
  bodyRef,
  focused,
  onFocus,
  onSend,
  onCancel,
}: {
  group: { quote: string; threads: Thread[] };
  bodyRef: React.RefObject<HTMLElement | null>;
  focused: string | null;
  onFocus: (thread: Thread) => void;
  onSend: (threadKey: string, body: string) => void;
  onCancel: (thread: Thread) => void;
}) {
  // Checked on render rather than remembered: answering a comment
  // usually rewrites the very passage it was about, so the common
  // case is that this becomes false a moment after the reply lands.
  const anchored = quoteStillPresent(bodyRef.current, group.quote);

  return (
    <li className="doc-conv-group">
      {/* The passage, once. Two conversations about the same sentence
          used to quote it back twice, which reads as a rendering
          fault before it reads as two conversations. */}
      <button
        type="button"
        className="doc-conv-anchor"
        onClick={() => onFocus(group.threads[0])}
      >
        {group.quote ? (
          <>
            <span className="doc-conv-quote">{group.quote}</span>
            {/* Said, not left to be noticed. A quote that no longer
                appears in the document simply stops lighting up, and
                a highlight that does nothing is indistinguishable
                from one that is broken. */}
            {!anchored && (
              <span className="doc-conv-moved">This passage has changed since</span>
            )}
          </>
        ) : (
          <span className="doc-conv-scope">About the whole document</span>
        )}
      </button>

      {group.threads.map((thread) => (
        <ThreadView
          key={thread.key}
          thread={thread}
          focused={focused === thread.key}
          onFocus={() => onFocus(thread)}
          onSend={(body) => onSend(thread.key, body)}
          onCancel={() => onCancel(thread)}
        />
      ))}
    </li>
  );
}

/** One conversation about that passage, with its own composer. */
function ThreadView({
  thread,
  focused,
  onFocus,
  onSend,
  onCancel,
}: {
  thread: Thread;
  focused: boolean;
  onFocus: () => void;
  onSend: (body: string) => void;
  onCancel: () => void;
}) {
  const running = isRunning(thread);

  /**
   * Anywhere on the conversation takes you to the passage.
   *
   * It used to be the quote alone, which made a small target sit
   * inside a large inert one: the sentence you are reading in the
   * card *is* the comment, and clicking it did nothing.
   *
   * The controls keep their own jobs. Clicking into the reply box has
   * to leave the page where it is — scrolling the document out from
   * under somebody who is starting to type is the one thing this must
   * not do.
   */
  const fromCard = (e: React.MouseEvent) => {
    const at = e.target as HTMLElement;
    if (at.closest("textarea, input, select")) return;
    const control = at.closest("button, a");
    if (control && !control.classList.contains("doc-conv-anchor")) return;
    onFocus();
  };

  return (
    <div
      className={focused ? "doc-conv-item doc-conv-focused" : "doc-conv-item"}
      data-thread={thread.key}
      onClick={fromCard}
    >
      {/* Which version it was left against, and where it got to.
          A thread on v12 beside one on v50 is a different kind of
          remark, and "accepted" is the difference between a question
          and a decision. */}
      {(thread.n || thread.status) && (
        <div className="doc-conv-meta">
          {thread.n ? <span className="doc-conv-at">v{thread.n}</span> : null}
          {thread.status && thread.status !== "open" && (
            <span className={`doc-conv-status is-${thread.status}`}>{thread.status}</span>
          )}
        </div>
      )}

      {thread.turns.map((turn) => (
        <TurnView key={turn.key} turn={turn} />
      ))}

      {thread.error && <div className="msg-error">{thread.error}</div>}

      {/* Settled threads are read, not continued. A reply box under
          a decision invites reopening it by accident. */}
      {(!thread.status || thread.status === "open") && (
        <ThreadComposer
          busy={running}
          first={thread.turns.length === 0}
          onSend={onSend}
          onCancel={onCancel}
        />
      )}
    </div>
  );
}

/**
 * The elbow joining an answer to the turn above it.
 *
 * A thread is a column of paragraphs, and an answer sitting under a
 * question looks exactly like a second question — the order is the
 * only thing saying otherwise, and order is what a reader loses
 * first in a thread with four turns in it. The line says which
 * remark this one is about without a sentence having to.
 *
 * Drawn rather than measured: the answer is always directly beneath
 * what it answers, so the two ends are a fixed distance apart and
 * the curve that joins them is the same curve every time. The
 * measured version of this exists for the passage-to-card line,
 * where the two ends are in different scroll containers.
 */
function ReplyHook() {
  const hook = useRef<SVGSVGElement>(null);
  const [reach, setReach] = useState(FALLBACK_REACH);

  // Measured, because the thing it reaches back to is a paragraph
  // somebody typed and there is no other way to know how tall that
  // is.
  //
  // Re-measured on every render, not once on mount. The rail settles
  // in several passes — the document's text arrives, the threads
  // reorder by anchor, the highlights paint — and a line measured
  // during the first of those is drawn to where the comment was
  // about to stop being. Measuring once left every hook reaching to
  // a point 38 pixels above the turn it was meant to meet, which
  // reads as a line to nothing in particular.
  //
  // Two rects per render is cheap. The guard below is what makes it
  // safe: state is only set when the number actually changed, so
  // this settles instead of re-rendering itself forever.
  useLayoutEffect(() => {
    const svg = hook.current;
    const mine = svg?.parentElement;
    // The turn this answers, which is the last thing a *person* said
    // — not simply the element above. Two agent answers to one
    // question sit one after the other, and anchoring to whatever is
    // directly above makes the second one reach back to the first
    // one's last line: a stub joining igni to igni, next to a
    // question neither of them appears to be about.
    const answered = personBefore(mine);
    if (!svg || !mine || !answered) return;

    const measure = () => {
      // Exception-safe: jsdom has no layout, and a rail that threw
      // here would take every thread on the page down with it.
      try {
        const from = answered.getBoundingClientRect();
        const to = mine.getBoundingClientRect();
        // From where the answered turn *ends*, not where it starts.
        // Drawn from its top, the line runs the whole height of
        // somebody's comment with their name sitting to the right of
        // it, which reads as a bracket around what they said rather
        // than a thread from it to the answer.
        const span = to.top - from.bottom;
        const next = span > 0 ? span + BYLINE_MIDDLE : FALLBACK_REACH;
        setReach((was) => (Math.abs(was - next) > 0.5 ? next : was));
      } catch {
        setReach((was) => (was === FALLBACK_REACH ? was : FALLBACK_REACH));
      }
    };
    measure();

    if (typeof ResizeObserver !== "function") return;
    const watch = new ResizeObserver(measure);
    watch.observe(answered);
    watch.observe(mine);
    return () => watch.disconnect();
  });

  // Drawn in pixels rather than scaled from a viewBox: a stretched
  // viewBox stretches the curve with it, and a quarter-circle three
  // paragraphs tall is an arc, not a corner.
  const corner = Math.min(CORNER, Math.max(2, reach - 2));
  return (
    <svg
      ref={hook}
      className="doc-conv-hook"
      width={14}
      height={reach}
      style={{ top: `${BYLINE_MIDDLE - reach}px` }}
      aria-hidden="true"
      focusable="false"
    >
      <path
        d={`M1 0 V${reach - corner} Q1 ${reach} ${1 + corner} ${reach} H13`}
      />
    </svg>
  );
}

/**
 * The last turn a person took before this one.
 *
 * Walks back over the agent's own answers. ``null`` when there is
 * nothing before it — an agent turn opening a thread, which happens
 * when a comment is projected back out of the store without the
 * remark that prompted it.
 */
export function personBefore(from: Element | null | undefined): Element | null {
  let at = from?.previousElementSibling ?? null;
  while (at && !at.classList.contains("doc-conv-body")) {
    at = at.previousElementSibling;
  }
  return at;
}

/** Where the line lands: the middle of the byline it leads to. */
const BYLINE_MIDDLE = 9;

/** How far it reaches when nothing can be measured. */
const FALLBACK_REACH = 18;

/** The radius of the corner at the bottom. */
const CORNER = 6;

/** The turn-back arrow beside the word Reply. */
function ReplyArrow() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="doc-thread-reply-icon">
      <path
        d="M6 4 2.5 7.5 6 11"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M2.5 7.5H9a4 4 0 0 1 4 4V13"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** ``sam@acme.test`` → ``sam``. What people call each other. */
function shortName(email: string): string {
  return email.split("@")[0] || email;
}

/** One turn — mine, or the answer to it. */
function TurnView({ turn }: { turn: Turn }) {
  // Named when it was somebody else. A turn written here needs no
  // byline — there is one person on this side of the screen — but a
  // thread read back out of the store is several people, and without
  // names it is one voice arguing with itself.
  const byline = turn.author ? (
    <span className="doc-conv-by">{shortName(turn.author)}</span>
  ) : null;

  // When it was said. A thread is an argument in order, and without
  // dates the order is the only thing carrying it — which breaks the
  // moment a passage is commented on twice a month apart and the two
  // remarks sit next to each other looking like one exchange.
  //
  // Relative in the rail and exact on hover: "3d ago" is the right
  // length for a column this narrow and the wrong amount of detail
  // for somebody working out what happened in what order.
  const when = turn.at ? (
    <time className="doc-conv-when" dateTime={turn.at} title={formatExact(turn.at)}>
      {formatRelative(turn.at)}
    </time>
  ) : null;

  if (turn.who === "me") {
    return (
      <p className="doc-conv-body">
        {byline}
        {turn.body}
        {when}
      </p>
    );
  }

  return (
    <div className="doc-conv-reply" aria-live="polite">
      {/* Always named, and named without waiting for an author field
          to arrive from the store — the agent has no account and
          never fills one in, so the version of this that keyed the
          byline on ``author`` printed igni's answer as an
          unattributed block of prose. In a thread where every other
          turn carries a name, the one paragraph with no name reads as
          part of whatever was said above it. */}
      <ReplyHook />
      <span className="doc-conv-by doc-conv-by-agent">igni</span>
      {when}
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

/**
 * The foot of a thread, in its two states.
 *
 * At rest it is the word Reply. Open, it is the box and the two ways
 * out of it. A box under every thread meant a rail of twenty
 * conversations carried twenty empty textareas and twenty send
 * buttons — more furniture than comment, and every one of them
 * looking like somewhere a reply had been started and abandoned.
 *
 * A thread with nothing in it yet opens straight into the box: it
 * only exists because somebody just asked for it, and asking them to
 * press Reply to the comment they are in the middle of writing is a
 * click that says nothing.
 */
function ThreadComposer({
  busy,
  first,
  onSend,
  onCancel,
}: {
  busy: boolean;
  /** Nothing has been said yet, so this is the opening remark. */
  first: boolean;
  onSend: (body: string) => void;
  /** Backing out of the box. Discards the thread if it was empty. */
  onCancel: () => void;
}) {
  const [body, setBody] = useState("");
  const [open, setOpen] = useState(first);
  const input = useRef<HTMLTextAreaElement>(null);

  // On opening, not only on mount: the point of pressing Reply is to
  // type, and a box that has to be clicked as well is a box that has
  // asked for the same decision twice.
  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const submit = () => {
    if (!body.trim() || busy) return;
    onSend(body);
    setBody("");
    // Back to the word Reply — including for the comment that opened
    // the thread. Leaving that one open would make a thread look
    // different depending on whether you happened to start it this
    // session or read it back from the store, which is not a
    // difference the reader has.
    setOpen(false);
  };

  const cancel = () => {
    setBody("");
    setOpen(false);
    onCancel();
  };

  if (!open) {
    return (
      <div className="doc-thread-foot">
        <button
          type="button"
          className="doc-thread-reply"
          onClick={() => setOpen(true)}
        >
          <ReplyArrow />
          Reply
        </button>
      </div>
    );
  }

  const asksAgent = mentionsIgni(body);

  return (
    <div className="doc-thread-compose">
      <textarea
        ref={input}
        className="doc-comment-input"
        rows={first ? 2 : 1}
        value={body}
        disabled={busy}
        placeholder={
          first ? "What should change, or what do you want to know?" : "Reply…"
        }
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          // Enter sends, because this is a message. Shift+Enter for a
          // newline, the same bargain the chat composer makes.
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
          // The other way out, for a keyboard. Only from an empty box:
          // Escape over a half-written reply would throw it away, and
          // there is no undo for that.
          if (e.key === "Escape" && !body.trim()) {
            e.preventDefault();
            cancel();
          }
        }}
      />
      {/* Under the box rather than beside it. Beside it the button
          had to align to one edge of a growing textarea and sat below
          its middle, and there was nowhere to say what it does. */}
      <div className="doc-thread-actions">
        <button
          type="button"
          className="btn btn-sm doc-thread-cancel"
          onClick={cancel}
        >
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-sm btn-primary doc-thread-send"
          disabled={busy || !body.trim()}
          onClick={submit}
        >
          {busy ? "…" : asksAgent ? "Ask igni" : first ? "Comment" : "Reply"}
        </button>
        {/* Said before sending, not after. Whether a model runs is the
            difference between a remark and an edit being proposed, and
            the rule that decides it is one word the writer either
            typed or did not. */}
        <span className="doc-thread-hint">
          {asksAgent
            ? "igni will answer here and may propose a change — nothing is applied until you accept it."
            : "Mention @igni to ask it to do something."}
        </span>
      </div>
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
