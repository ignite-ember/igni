import { useEffect, useRef, useState, type RefObject } from "react";

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
}: {
  scrollRef: RefObject<HTMLElement | null>;
}) {
  const [thumb, setThumb] = useState<{ top: number; height: number; visible: boolean }>({
    top: 0,
    height: 0,
    visible: false,
  });
  const fadeTimer = useRef<number | null>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    // Padding so the thumb never touches the chrome edges (header
    // blur up top, composer / footer / sidebar end down bottom).
    // The thumb's travel range shrinks by the same amount, so the
    // bottom of the list still maps visibly to the bottom of the
    // track. 32 px is enough to read clearly as inset on both the
    // narrow sidebar rail and the wider conversation column.
    const PAD = 32;

    const update = () => {
      const { scrollTop, scrollHeight, clientHeight } = el;
      // Hide entirely when there's nothing to scroll.
      if (scrollHeight <= clientHeight + 1) {
        setThumb((t) => (t.visible ? { ...t, visible: false } : t));
        return;
      }
      const track = Math.max(0, clientHeight - PAD * 2);
      const ratio = clientHeight / scrollHeight;
      // Minimum thumb height so it stays grabbable even on very
      // long lists; bounded above by the available track length.
      const thumbH = Math.max(24, Math.min(track, clientHeight * ratio));
      const maxScroll = scrollHeight - clientHeight;
      const maxThumbTop = Math.max(0, track - thumbH);
      const top = PAD + (maxScroll > 0 ? (scrollTop / maxScroll) * maxThumbTop : 0);
      setThumb({ top, height: thumbH, visible: true });
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
  }, [scrollRef]);

  if (!thumb.visible) return null;
  return (
    <div
      className={`scroll-indicator${active ? " active" : ""}`}
      style={{ top: thumb.top, height: thumb.height }}
      aria-hidden="true"
    />
  );
}
