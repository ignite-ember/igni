import { useEffect, useRef, useState, type RefObject } from "react";

/** Padding so the thumb never touches the chrome edges (header
 *  blur up top, composer / footer / sidebar end down bottom). The
 *  thumb's travel range shrinks by the same amount, so the bottom
 *  of the list still maps visibly to the bottom of the track. 32 px
 *  reads as inset on both narrow sidebar rails and wider columns. */
export const SCROLL_INDICATOR_PAD = 32;
/** Minimum thumb height so it stays grabbable even on very long
 *  lists. macOS overlay scrollbar uses ~14px; we lean larger so
 *  the thumb stays a useful position indicator. */
export const SCROLL_INDICATOR_MIN_THUMB = 24;

export interface ThumbRect {
  top: number;
  height: number;
  visible: boolean;
}

/** Pure helper: derive the thumb rect from a scroll container's
 *  size. Extracted so the math is testable without DOM scroll
 *  events. Returns ``{visible:false}`` when nothing can scroll
 *  (the thumb hides entirely in that case rather than rendering
 *  a useless full-track rectangle). */
export function computeThumb(
  scrollTop: number,
  scrollHeight: number,
  clientHeight: number,
  pad: number = SCROLL_INDICATOR_PAD,
): ThumbRect {
  // Hide entirely when there's nothing to scroll. The +1 absorbs
  // sub-pixel rounding so a "fits exactly" list doesn't flicker
  // the thumb in and out.
  if (scrollHeight <= clientHeight + 1) {
    return { top: 0, height: 0, visible: false };
  }
  const track = Math.max(0, clientHeight - pad * 2);
  const ratio = clientHeight / scrollHeight;
  // Minimum thumb height (grabbable on long lists); bounded above
  // by the available track length.
  const thumbH = Math.max(SCROLL_INDICATOR_MIN_THUMB, Math.min(track, clientHeight * ratio));
  const maxScroll = scrollHeight - clientHeight;
  const maxThumbTop = Math.max(0, track - thumbH);
  const top = pad + (maxScroll > 0 ? (scrollTop / maxScroll) * maxThumbTop : 0);
  return { top, height: thumbH, visible: true };
}

/**
 * Read-only scroll-position indicator rendered on top of the
 * frosted headers. The native scrollbar lives inside the
 * scrollable element (z-index: 1) and gets blurred by the
 * progressive-blur stacks sitting above it (z-index: 5–10), so
 * the user can't see where they are in the list while scrolling.
 * This component renders a thumb whose position tracks the
 * container's ``scrollTop`` at a higher z-index than the blur.
 *
 * The native scrollbar is hidden via the ``scrollbar-width: none``
 * + ``::-webkit-scrollbar { display: none }`` rules on the parent
 * scroll element. This indicator is read-only — drag-to-scroll
 * would need pointer-event wiring and isn't worth the complexity
 * for the chat / sessions surfaces.
 */
export function ScrollIndicator({
  scrollRef,
  element,
}: {
  scrollRef?: RefObject<HTMLElement | null>;
  /** Alternative to ``scrollRef`` for callers that obtain the scroll
   *  element asynchronously (e.g. Virtuoso's ``scrollerRef`` only
   *  fires after the first render). State-backed so this component
   *  re-runs its bind effect when the element appears. */
  element?: HTMLElement | null;
}) {
  const [thumb, setThumb] = useState<{ top: number; height: number; visible: boolean }>({
    top: 0,
    height: 0,
    visible: false,
  });
  const fadeTimer = useRef<number | null>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const el = element ?? scrollRef?.current ?? null;
    if (!el) return;

    const update = () => {
      const rect = computeThumb(el.scrollTop, el.scrollHeight, el.clientHeight);
      if (!rect.visible) {
        setThumb((t) => (t.visible ? { ...t, visible: false } : t));
        return;
      }
      setThumb(rect);
    };

    const onScroll = () => {
      update();
      setActive(true);
      if (fadeTimer.current !== null) window.clearTimeout(fadeTimer.current);
      // Fade the thumb out shortly after scrolling stops, the same
      // way the macOS overlay scrollbar behaves.
      fadeTimer.current = window.setTimeout(() => setActive(false), 700);
    };

    el.addEventListener("scroll", onScroll, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(el);
    update();

    return () => {
      el.removeEventListener("scroll", onScroll);
      ro.disconnect();
      if (fadeTimer.current !== null) window.clearTimeout(fadeTimer.current);
    };
  }, [scrollRef, element]);

  if (!thumb.visible) return null;
  return (
    <div
      className={`scroll-indicator${active ? " active" : ""}`}
      style={{ top: thumb.top, height: thumb.height }}
      aria-hidden="true"
    />
  );
}
