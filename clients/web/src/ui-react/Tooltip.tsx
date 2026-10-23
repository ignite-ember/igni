/**
 * What an icon means, on hover and on focus.
 *
 * A toolbar of glyphs is quicker to scan and slower to learn, and
 * the moment somebody needs the word is the moment their pointer is
 * already on the thing. The browser's own ``title`` does technically
 * say it — after a second and a half, in the operating system's
 * font, at the pointer rather than at the control, and never for
 * somebody on a keyboard. That is not an explanation, it is a
 * consolation prize.
 *
 * **It replaces ``title``, it does not sit beside it.** Leaving both
 * on gives two tooltips saying the same thing a second apart.
 *
 * **Focus shows it immediately; hover waits.** A pointer crossing a
 * row of buttons on its way somewhere else should not leave a trail
 * of popups, but a keyboard user arriving at a control has asked for
 * it by arriving.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** How long a pointer must rest before the word appears. */
const DWELL_MS = 350;

/** Gap between the control and the bubble. */
const OFFSET = 8;

/** Keeps the bubble off the window edges. */
const MARGIN = 8;

interface At {
  left: number;
  top: number;
  /** Below the control, or above it when there is no room below. */
  below: boolean;
}

export interface TooltipProps {
  /** The word. Nothing is rendered when it is empty. */
  text: string;
  children: React.ReactNode;
}

export function Tooltip({ text, children }: TooltipProps) {
  const wrap = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const timer = useRef<number | null>(null);
  const [at, setAt] = useState<At | null>(null);
  const id = useId();

  const hide = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setAt(null);
  }, []);

  const place = useCallback(() => {
    // The control, not the pointer. A bubble that follows the cursor
    // is a bubble that covers the next control along.
    const anchor = wrap.current?.firstElementChild ?? wrap.current;
    const box = anchor?.getBoundingClientRect();
    if (!box) return;
    const below = box.bottom + OFFSET + 34 < window.innerHeight;
    setAt({
      left: box.left + box.width / 2,
      top: below ? box.bottom + OFFSET : box.top - OFFSET,
      below,
    });
  }, []);

  const show = useCallback(
    (immediately: boolean) => {
      if (!text) return;
      if (timer.current !== null) window.clearTimeout(timer.current);
      if (immediately) {
        place();
        return;
      }
      timer.current = window.setTimeout(place, DWELL_MS);
    },
    [text, place],
  );

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  // Escape closes it, and so does anything that scrolls it away from
  // the control it belongs to.
  useEffect(() => {
    if (!at) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [at, hide]);

  // Nudged back inside the window once its width is known. Measured
  // rather than guessed: the text is a sentence in some places and a
  // word in others.
  useEffect(() => {
    const el = bubble.current;
    if (!at || !el) return;
    const box = el.getBoundingClientRect();
    const over = box.right - (window.innerWidth - MARGIN);
    const under = MARGIN - box.left;
    if (over > 0)
      el.style.transform = `translate(calc(-50% - ${over}px), ${at.below ? "0" : "-100%"})`;
    else if (under > 0)
      el.style.transform = `translate(calc(-50% + ${under}px), ${at.below ? "0" : "-100%"})`;
  }, [at]);

  // Listened for on the control itself, not on the wrapper.
  //
  // The wrapper is ``display: contents`` so it does not become a box
  // in a flex row — every toolbar it is used in would otherwise gain
  // a layer of spacing nobody designed. But an element with no box
  // is an element the browser never hits with a pointer, so React's
  // onPointerEnter on the wrapper fired in jsdom, where there is no
  // hit-testing, and never once in a real browser. Focus worked the
  // whole time, which is what made it look like a timing problem.
  useEffect(() => {
    const control = wrap.current?.firstElementChild;
    if (!control) return;
    const enter = () => show(false);
    const now = () => show(true);
    control.addEventListener("pointerenter", enter);
    control.addEventListener("pointerleave", hide);
    control.addEventListener("pointerdown", hide);
    control.addEventListener("focus", now);
    control.addEventListener("blur", hide);
    return () => {
      control.removeEventListener("pointerenter", enter);
      control.removeEventListener("pointerleave", hide);
      control.removeEventListener("pointerdown", hide);
      control.removeEventListener("focus", now);
      control.removeEventListener("blur", hide);
    };
  }, [show, hide]);

  return (
    <>
      <span
        ref={wrap}
        className="tip-anchor"
        aria-describedby={at ? id : undefined}
      >
        {children}
      </span>
      {at &&
        createPortal(
          <div
            ref={bubble}
            id={id}
            role="tooltip"
            className={`tip ${at.below ? "tip-below" : "tip-above"}`}
            style={{
              left: at.left,
              top: at.top,
              transform: `translate(-50%, ${at.below ? "0" : "-100%"})`,
            }}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}
