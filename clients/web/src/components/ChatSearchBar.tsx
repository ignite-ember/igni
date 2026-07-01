import { memo, useCallback, useEffect, useRef, useState } from "react";
import type { ChatSearchMatch, EmberClient } from "../protocol/client";

/** Find-in-conversation bar. Search runs on the BE against the Agno
 *  SQLite session (so a 10k-turn dialogue doesn't jank the JS thread),
 *  results carry ``history_index`` from ``get_chat_history``'s
 *  emission order, and the parent translates those to FE item
 *  indices via the parallel map built at session-load time.
 *
 *  Keyboard:
 *    • Esc — close
 *    • Enter / Down — next match
 *    • Shift+Enter / Up — previous match
 */
export interface ChatSearchBarProps {
  client: EmberClient;
  sessionId: string;
  /** Maps a BE ``history_index`` to the FE item index (or -1 if the
   *  history turn was filtered out by ``restoredItem``). */
  historyIndexToItemIndex: number[];
  /** Total items currently in the chat — used as the upper bound for
   *  index translation when search runs while live items have been
   *  appended after the historical load. */
  liveItemCount: number;
  /** Called when the user activates a result. ``itemIndex`` is the
   *  index into the FE's items array. */
  onJumpTo: (itemIndex: number, match: ChatSearchMatch) => void;
  onClose: () => void;
}

const DEBOUNCE_MS = 150;
const LIMIT = 50;

function MagnifierIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0 }}
    >
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5L13.5 13.5" />
    </svg>
  );
}

function ChevronGlyph({ up }: { up?: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: "block", transform: up ? "rotate(180deg)" : undefined }}
    >
      <path d="M3 6l5 5 5-5" />
    </svg>
  );
}

function CloseGlyph() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: "block" }}
    >
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

/** Format a turn's ``created_at`` (epoch seconds) for the result row.
 *  Recent matches read as a relative ("2m ago", "3h ago") so the
 *  user can scan when a turn happened without parsing dates; older
 *  ones fall back to the locale-formatted date + time. ``0`` (no
 *  timestamp on the persisted message) renders as empty. */
export function formatTurnTime(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const ts = epochSeconds * 1000;
  const diff = Date.now() - ts;
  if (diff < 0) {
    // Clock skew between client/server — show the bare time of day.
    return new Date(ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ts).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Strip the raw-text noise that would otherwise show up in the
 *  search-result snippet — wrapper tags (``<think>``,
 *  ``<loop-iteration …>``, ``<attached-files>``, ``<system-context>``),
 *  inline-code-paste markers, ``@code:<id>`` pill placeholders, and
 *  the most common Markdown emphasis / heading characters. Applied
 *  to the ``before``/``after`` halves of the snippet only — the
 *  matched substring renders verbatim under ``<mark>`` so the user
 *  sees what they actually typed. */
export function cleanSnippetText(s: string): string {
  return s
    // Drop the entire ``<system-context>…</system-context>`` and
    // ``<attached-files>…</attached-files>`` BLOCKS, contents
    // included — mirrors what ``restoredItem`` strips so the
    // search-result snippet matches what the user sees in the
    // chat (no leaked "Current datetime: …" prefix).
    .replace(/<system-context>[\s\S]*?<\/system-context>\s*/g, "")
    .replace(/<attached-files>[\s\S]*?<\/attached-files>\s*/g, "")
    .replace(/<\/?think>/g, "")
    .replace(/<\/?loop-iteration[^>]*>/g, "")
    // Collapse [code-paste …] … [/code-paste] blocks (multi-line)
    // to a single inline marker so the snippet doesn't include the
    // pasted source.
    .replace(/\[code-paste[^\]]*\][\s\S]*?\[\/code-paste\]/g, "[code]")
    // @code:<id> tokens — render as the friendlier "[code]".
    .replace(/@code:\S+/g, "[code]")
    // Markdown bold/italic markers (keep the inner text).
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/(?<![*_])([*_])([^*_\n]+?)\1(?![*_])/g, "$2")
    // Inline-code backticks — keep content, drop ticks.
    .replace(/`+([^`]+)`+/g, "$1")
    // Leading heading hashes (``# Foo`` → ``Foo``).
    .replace(/^#+\s+/gm, "")
    // Squash newlines + runs of whitespace down to single spaces.
    .replace(/\s+/g, " ");
}

/** Translate a BE history_index to a FE item index. Falls back to
 *  the live tail when the map doesn't cover the index (e.g., the
 *  user opened search after some live items were appended). */
export function translateIndex(
  historyIndex: number,
  map: number[],
  liveItemCount: number,
): number {
  if (historyIndex >= 0 && historyIndex < map.length) {
    const mapped = map[historyIndex];
    if (mapped >= 0 && mapped < liveItemCount) return mapped;
  }
  return -1;
}

export const ChatSearchBar = memo(function ChatSearchBar({
  client,
  sessionId,
  historyIndexToItemIndex,
  liveItemCount,
  onJumpTo,
  onClose,
}: ChatSearchBarProps) {
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ChatSearchMatch[]>([]);
  const [activeIdx, setActiveIdx] = useState(0);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const seqRef = useRef(0);

  // Autofocus on mount.
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  // Debounced search.
  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setMatches([]);
      setActiveIdx(0);
      setPending(false);
      return;
    }
    setPending(true);
    const seq = ++seqRef.current;
    const t = window.setTimeout(async () => {
      try {
        const res = await client.searchChat(sessionId, trimmed, LIMIT);
        if (seq !== seqRef.current) return; // stale
        setMatches(res);
        setActiveIdx(0);
      } catch {
        if (seq !== seqRef.current) return;
        setMatches([]);
      } finally {
        if (seq === seqRef.current) setPending(false);
      }
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [query, sessionId, client]);

  const jump = useCallback(
    (i: number, andClose = false) => {
      const m = matches[i];
      if (!m) return;
      const itemIndex = translateIndex(
        m.history_index,
        historyIndexToItemIndex,
        liveItemCount,
      );
      if (itemIndex < 0) return;
      setActiveIdx(i);
      onJumpTo(itemIndex, m);
      // Clicking a specific result reads as "take me there" —
      // dismiss the bar. Keyboard nav (Enter / arrows / chevrons)
      // leaves it open so the user can keep browsing matches.
      if (andClose) onClose();
    },
    [matches, historyIndexToItemIndex, liveItemCount, onJumpTo, onClose],
  );

  const nextMatch = useCallback(() => {
    if (!matches.length) return;
    const i = (activeIdx + 1) % matches.length;
    jump(i);
  }, [matches.length, activeIdx, jump]);

  const prevMatch = useCallback(() => {
    if (!matches.length) return;
    const i = (activeIdx - 1 + matches.length) % matches.length;
    jump(i);
  }, [matches.length, activeIdx, jump]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (e.shiftKey) prevMatch();
        else nextMatch();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        nextMatch();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        prevMatch();
      }
    },
    [onClose, nextMatch, prevMatch],
  );

  const status = (() => {
    if (!query.trim()) return "Type to search this conversation";
    if (pending && !matches.length) return "Searching…";
    if (!matches.length) return "No matches";
    return `${activeIdx + 1} / ${matches.length}${matches.length === LIMIT ? "+" : ""}`;
  })();

  return (
    <div className="chat-search-bar" role="search">
      <div className="chat-search-row">
        <span className="chat-search-icon">
          <MagnifierIcon />
        </span>
        <input
          ref={inputRef}
          className="chat-search-input"
          type="text"
          value={query}
          placeholder="Find in conversation"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          spellCheck={false}
          autoComplete="off"
        />
        <span className="chat-search-status" aria-live="polite">
          {status}
        </span>
        <button
          type="button"
          className="chat-search-nav"
          onClick={prevMatch}
          disabled={!matches.length}
          title="Previous match (Shift+Enter)"
          aria-label="Previous match"
        >
          <ChevronGlyph up />
        </button>
        <button
          type="button"
          className="chat-search-nav"
          onClick={nextMatch}
          disabled={!matches.length}
          title="Next match (Enter)"
          aria-label="Next match"
        >
          <ChevronGlyph />
        </button>
        <button
          type="button"
          className="chat-search-close"
          onClick={onClose}
          title="Close (Esc)"
          aria-label="Close search"
        >
          <CloseGlyph />
        </button>
      </div>
      {matches.length > 0 && (
        <ul className="chat-search-results">
          {matches.map((m, i) => {
            const itemIndex = translateIndex(
              m.history_index,
              historyIndexToItemIndex,
              liveItemCount,
            );
            const unreachable = itemIndex < 0;
            // Render the matched substring as the user typed it; clean
            // only the surrounding context so wrapper tags / pill
            // placeholders / markdown markers don't leak into the row.
            const before = cleanSnippetText(m.snippet.slice(0, m.match_start));
            const hit = m.snippet.slice(m.match_start, m.match_end);
            const after = cleanSnippetText(m.snippet.slice(m.match_end));
            return (
              <li
                key={`${m.history_index}-${i}`}
                className={`chat-search-result${i === activeIdx ? " active" : ""}${unreachable ? " unreachable" : ""}`}
                onClick={() => !unreachable && jump(i, true)}
                title={unreachable ? "This match isn't reachable in the current view" : undefined}
              >
                <span className={`chat-search-role chat-search-role-${m.role}`}>
                  {m.role === "user" ? "you" : m.role === "assistant" ? "ember" : m.role}
                </span>
                <span className="chat-search-snippet">
                  {before}
                  <mark>{hit}</mark>
                  {after}
                </span>
                {m.created_at > 0 && (
                  <span
                    className="chat-search-time"
                    title={new Date(m.created_at * 1000).toLocaleString()}
                  >
                    {formatTurnTime(m.created_at)}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
});
