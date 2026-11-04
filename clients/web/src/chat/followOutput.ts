/**
 * Virtuoso's ``followOutput`` for a chat list, held off until Virtuoso's
 * "at bottom" agrees with the page.
 *
 * V6. Under eight workers a 60-row list opened at its end (scrollTop 1606)
 * in about half the runs. Virtuoso reads "at bottom" from its own record of
 * the scroller, which ResizeObserver keeps up to date; on a loaded machine
 * those callbacks are late, and for a moment after mount the record still
 * says "empty scroller", so Virtuoso reports at bottom = true while the
 * rows are already on the page. The list's ``atBottomStateChange`` handler
 * re-renders, every re-render republishes Virtuoso's props, and a props
 * refresh with ``followOutput`` set arms "keep the bottom when the size
 * grows" for 100 ms. The late measurement lands inside it ("size
 * increased"), and Virtuoso scrolls to the end on its own.
 *
 * So until Virtuoso and the page agree, once the rows have been measured,
 * ``followOutput`` is ``false`` (nothing arms) and a "true" the page
 * contradicts is passed on as ``false``. While they disagree the page is
 * looked at again each frame, because Virtuoso does not repeat a value it
 * has already reported (for two seconds at most). From the first agreement
 * on, Virtuoso's word is passed through as it is.
 *
 * Pinned by ``e2e/chat-scroll.spec.ts`` ("measured late").
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface ChatFollow {
  followOutput: "auto" | false;
  atBottomStateChange: (atBottom: boolean) => void;
  totalListHeightChanged: (height: number) => void;
  scrollerRef: (el: HTMLElement | Window | null) => void;
}

/**
 * ``count`` is the list's length: the App shows no list while it is 0, so
 * 0 starts over. ``threshold`` is the list's ``atBottomThreshold``.
 * ``onAtBottom`` gets what ``atBottomStateChange`` would have.
 */
export function useChatFollow(
  count: number,
  threshold: number,
  onAtBottom: (atBottom: boolean) => void,
): ChatFollow {
  const [agreed, setAgreed] = useState(false);
  const agreedRef = useRef(false);
  const scroller = useRef<HTMLElement | null>(null);
  const measured = useRef(false);
  // What Virtuoso last said, and what was last passed on.
  const said = useRef<boolean | null>(null);
  const told = useRef<boolean | null>(null);
  const frame = useRef<number | null>(null);
  const looked = useRef(0);
  const onAtBottomRef = useRef(onAtBottom);
  onAtBottomRef.current = onAtBottom;

  const stopLooking = () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  };

  const tell = (atBottom: boolean) => {
    if (told.current === atBottom) return;
    told.current = atBottom;
    onAtBottomRef.current(atBottom);
  };

  const pageAtBottom = (): boolean | null => {
    const el = scroller.current;
    if (!el) return null;
    return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  };

  // Before agreement: pass on only what the page confirms, and keep
  // looking until it does.
  const reconcile = useCallback(() => {
    frame.current = null;
    if (agreedRef.current) return;
    const virtuoso = said.current;
    const page = pageAtBottom();
    if (virtuoso === null) return;
    if (page === null) {
      tell(virtuoso);
      return;
    }
    looked.current += 1;
    // Two seconds of disagreement: Virtuoso is not catching up because
    // it has nothing to catch up on, so its word stands.
    if ((measured.current && virtuoso === page) || looked.current > 120) {
      agreedRef.current = true;
      setAgreed(true);
      tell(virtuoso);
      return;
    }
    tell(virtuoso && page);
    frame.current = requestAnimationFrame(reconcile);
  }, [threshold]);

  const atBottomStateChange = useCallback(
    (atBottom: boolean) => {
      said.current = atBottom;
      // Each report from Virtuoso is passed on; only the frame-by-frame
      // looks below stay quiet when nothing changed. Something else may
      // have told the consumer in between (the App's scrollToBottom).
      told.current = null;
      if (agreedRef.current) {
        tell(atBottom);
        return;
      }
      stopLooking();
      reconcile();
    },
    [reconcile],
  );

  const totalListHeightChanged = useCallback((height: number) => {
    if (height > 0) measured.current = true;
  }, []);

  const scrollerRef = useCallback((el: HTMLElement | Window | null) => {
    scroller.current = el instanceof HTMLElement ? el : null;
  }, []);

  const empty = count === 0;
  useEffect(() => {
    if (!empty) return;
    stopLooking();
    agreedRef.current = false;
    measured.current = false;
    looked.current = 0;
    said.current = null;
    told.current = null;
    setAgreed(false);
  }, [empty]);
  useEffect(() => stopLooking, []);

  return {
    followOutput: agreed ? "auto" : false,
    atBottomStateChange,
    totalListHeightChanged,
    scrollerRef,
  };
}
