/**
 * Headless scroll sandbox for the chat list.
 *
 * Reachable at ``?demo=chat-scroll`` (see main.tsx). Mirrors the
 * real App's Virtuoso wiring — same ``followOutput="auto"`` policy,
 * same ``atBottomStateChange`` hook — minus the BE coupling. Used
 * by Playwright (``e2e/chat-scroll.spec.ts``) to pin the behavior
 * that a new item appended while the user is at the bottom keeps
 * the bottom in view.
 *
 * The page exposes two affordances the tests drive:
 *   • An "Append message" button — adds one row.
 *   • A small status line: ``atBottom: <true|false>`` — surfaces
 *     Virtuoso's atBottom state so assertions don't have to
 *     measure pixels.
 *
 * Keep the rendered row simple (one line of text) so scroll
 * measurements don't depend on font metrics.
 */

import { useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";

const SEED_COUNT = 60;
const SEED_ITEMS = Array.from({ length: SEED_COUNT }, (_, i) => ({
  id: i + 1,
  text: `seed message #${i + 1}`,
}));

export function ChatScrollDemo() {
  const [items, setItems] = useState(SEED_ITEMS);
  const [atBottom, setAtBottom] = useState(false);
  const virtuoso = useRef<VirtuosoHandle>(null);

  const append = () => {
    setItems((prev) => [
      ...prev,
      { id: prev.length + 1, text: `appended message #${prev.length + 1}` },
    ]);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <header
        style={{
          padding: "10px 14px",
          borderBottom: "1px solid var(--border, #2a2f3a)",
          display: "flex",
          gap: 12,
          alignItems: "center",
          fontFamily: "system-ui, sans-serif",
          fontSize: 13,
        }}
      >
        <strong>Chat scroll sandbox</strong>
        <span data-testid="at-bottom-flag">atBottom: {String(atBottom)}</span>
        <span data-testid="item-count">items: {items.length}</span>
        <button
          data-testid="append-btn"
          onClick={append}
          style={{
            padding: "4px 10px",
            background: "var(--accent, #4a9eff)",
            color: "white",
            border: "none",
            borderRadius: 4,
            cursor: "pointer",
          }}
        >
          Append message
        </button>
        <button
          data-testid="scroll-top-btn"
          onClick={() => virtuoso.current?.scrollToIndex({ index: 0, behavior: "auto" })}
          style={{
            padding: "4px 10px",
            background: "transparent",
            color: "inherit",
            border: "1px solid var(--border, #2a2f3a)",
            borderRadius: 4,
            cursor: "pointer",
          }}
        >
          Scroll to top
        </button>
        <button
          data-testid="scroll-bottom-btn"
          onClick={() =>
            virtuoso.current?.scrollToIndex({
              index: "LAST",
              align: "end",
              behavior: "auto",
            })
          }
          style={{
            padding: "4px 10px",
            background: "transparent",
            color: "inherit",
            border: "1px solid var(--border, #2a2f3a)",
            borderRadius: 4,
            cursor: "pointer",
          }}
        >
          Scroll to bottom
        </button>
      </header>
      <div style={{ flex: 1, minHeight: 0 }} data-testid="scroller-wrap">
        <Virtuoso
          ref={virtuoso}
          data={items}
          computeItemKey={(_idx, item) => item.id}
          followOutput="auto"
          // Default ``atBottomThreshold`` is 4px which is too
          // tight in practice — Virtuoso frequently misses the
          // "at bottom" transition by a sub-pixel after a layout
          // change (composer growing, image loading, etc),
          // ``followOutput="auto"`` then bails on the follow
          // because its sample reads false. 50px is generous
          // without papering over a real "scrolled up" intent.
          atBottomThreshold={50}
          atBottomStateChange={setAtBottom}
          itemContent={(_idx, item) => (
            <div
              data-testid={`row-${item.id}`}
              style={{
                padding: "8px 14px",
                borderBottom: "1px solid var(--border-soft, #1d2028)",
                fontFamily: "system-ui, sans-serif",
                fontSize: 13,
              }}
            >
              {item.text}
            </div>
          )}
        />
      </div>
    </div>
  );
}
