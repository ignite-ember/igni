/**
 * The rule between two columns, made draggable.
 *
 * The hierarchy and the conversation are docked either side of the
 * document, and how much room each deserves is not a thing this app
 * can know: it depends on how deep somebody's folders go and how
 * much argument a passage has attracted. So it is theirs to set.
 *
 * **It drives a CSS variable, not React state.** A pointermove
 * handler that re-rendered the rail would re-render a hundred
 * threads per frame, and the drag would stutter on exactly the
 * documents busy enough to need resizing. Writing the variable lets
 * the browser reflow the columns and leaves the tree untouched.
 *
 * **The width is remembered per column.** Somebody who widens the
 * conversation has decided something about how they read, and asking
 * again next time is asking them to decide it again.
 */
import { useCallback, useEffect, useRef } from "react";

/** Which edge the handle sits on, and therefore which way is wider. */
export type Side = "left" | "right";

export interface ColumnHandleProps {
  /** The custom property this handle sets, e.g. ``--tree-width``. */
  variable: string;
  /** ``left`` widens as the pointer moves right; ``right`` inverts. */
  side: Side;
  /** Narrowest useful width, in pixels. */
  min: number;
  /** Widest, in pixels. Also clamped to the window below. */
  max: number;
  /** Where the remembered width is kept. */
  storageKey: string;
  /** Said to a screen reader, and shown on hover. */
  label: string;
}

/**
 * How much of the window a docked column may take, whatever its own
 * maximum says. Two columns dragged wide on a narrow laptop would
 * otherwise leave the document a few characters across, which is a
 * state the reader has to drag their way back out of.
 */
const MOST_OF_WINDOW = 0.4;

/** Read a remembered width, ignoring anything that is not a number. */
export function rememberedWidth(storageKey: string): number | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const px = Number.parseFloat(raw);
    return Number.isFinite(px) && px > 0 ? px : null;
  } catch {
    // Private browsing, a disabled store, a quota. A column at its
    // default width is a working column.
    return null;
  }
}

/** The width to use, clamped to what this window can spare. */
export function allowedWidth(px: number, min: number, max: number, windowWidth: number): number {
  const ceiling = Math.min(max, Math.round(windowWidth * MOST_OF_WINDOW));
  // ``min`` wins over the ceiling on a very narrow window: a column
  // below its minimum is unreadable, and unreadable is worse than
  // wide.
  return Math.round(Math.max(min, Math.min(px, Math.max(min, ceiling))));
}

export function ColumnHandle({
  variable,
  side,
  min,
  max,
  storageKey,
  label,
}: ColumnHandleProps) {
  const dragging = useRef<{ startX: number; startWidth: number } | null>(null);

  const apply = useCallback(
    (px: number) => {
      const width = allowedWidth(px, min, max, window.innerWidth);
      document.documentElement.style.setProperty(variable, `${width}px`);
      return width;
    },
    [variable, min, max],
  );

  // What was remembered, on the way in. Applied to the document
  // element rather than held in state for the same reason the drag
  // is: nothing above this needs to re-render because a column is
  // 40px wider.
  useEffect(() => {
    const saved = rememberedWidth(storageKey);
    if (saved !== null) apply(saved);
  }, [storageKey, apply]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const column = e.currentTarget.parentElement;
    if (!column) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragging.current = {
      startX: e.clientX,
      startWidth: column.getBoundingClientRect().width,
    };
    // Said on the root so it survives the pointer leaving the strip,
    // which it does on any drag faster than the reflow.
    document.documentElement.classList.add("is-resizing-column");
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const from = dragging.current;
    if (!from) return;
    const travelled = e.clientX - from.startX;
    apply(from.startWidth + (side === "left" ? travelled : -travelled));
  };

  const finish = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    dragging.current = null;
    document.documentElement.classList.remove("is-resizing-column");
    try {
      const now = document.documentElement.style.getPropertyValue(variable);
      if (now) window.localStorage.setItem(storageKey, now.replace("px", ""));
    } catch {
      // Not worth interrupting a drag that otherwise worked.
    }
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  // Arrow keys as well as a pointer. A 6px strip is a hard target
  // with a trackpad and an impossible one without a mouse, and this
  // is the only way to change a column that is otherwise fixed.
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 48 : 12;
    const column = e.currentTarget.parentElement;
    if (!column) return;
    const width = column.getBoundingClientRect().width;
    const wider = side === "left" ? "ArrowRight" : "ArrowLeft";
    const narrower = side === "left" ? "ArrowLeft" : "ArrowRight";
    let next: number | null = null;
    if (e.key === wider) next = width + step;
    else if (e.key === narrower) next = width - step;
    if (next === null) return;
    e.preventDefault();
    const applied = apply(next);
    try {
      window.localStorage.setItem(storageKey, String(applied));
    } catch {
      /* see above */
    }
  };

  return (
    <div
      className={`col-handle col-handle-${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={label}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finish}
      onPointerCancel={finish}
      onKeyDown={onKeyDown}
      // Double-click puts it back. A column dragged somewhere silly
      // is otherwise a slow drag back to a width nobody remembers.
      onDoubleClick={() => {
        document.documentElement.style.removeProperty(variable);
        try {
          window.localStorage.removeItem(storageKey);
        } catch {
          /* see above */
        }
      }}
    />
  );
}
