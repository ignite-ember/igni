/**
 * Two panes and a rule you can move.
 *
 * Both extensions built against this floor wanted one and neither
 * could have it: `ColumnHandle` is 180 lines in the app, does exactly
 * this, and was not published. `examples/release-notes` hand-rolls
 * the grid, the pointer capture, the clamping and the separator, and
 * says so in its README. Every two-pane extension would write that
 * again, differently — which is how two panels in one product end up
 * resizing differently.
 *
 * **It drives a CSS variable, not React state.** A `pointermove`
 * handler that re-rendered would re-render the whole document on
 * every frame of the drag, and would stutter on exactly the documents
 * busy enough to be worth resizing. Writing the variable lets the
 * browser reflow and leaves the tree alone.
 *
 * **It does not remember the width**, and that is the deliberate
 * difference from the app's version. An extension is served from an
 * ephemeral port, a port is part of an origin, so its `localStorage`
 * is wiped whenever the backend restarts — persisting there would be
 * a promise this cannot keep. `onResize` hands the width back
 * instead, and an extension that wants it remembered puts it
 * somewhere that survives: `bridge.setLocation`, or its own backend.
 *
 * The other difference is `aria-valuenow`. A focusable `separator`
 * that never says where it is tells a screen-reader user that
 * something is adjustable and not what it is adjusted to.
 */
import {
  useCallback,
  useId,
  useRef,
  useState,
  type HTMLAttributes,
  type PointerEvent as ReactPointerEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Keyboard step, and the larger one with Shift held. */
const STEP = 12;
const BIG_STEP = 48;

/**
 * How much of the window one pane may take, whatever `max` says.
 *
 * A pane dragged wide on a narrow laptop otherwise leaves the other
 * a few characters across — a state the reader has to drag their way
 * back out of.
 */
const MOST_OF_WINDOW = 0.6;

export interface SplitProps extends Omit<HTMLAttributes<HTMLDivElement>, "onResize"> {
  /** The fixed-width pane. */
  aside: ReactNode;
  /** The pane that takes what is left. */
  children: ReactNode;
  /** Which side `aside` is on. Defaults to the left. */
  side?: "start" | "end";
  /** Starting width of `aside`, in pixels. */
  defaultWidth?: number;
  min?: number;
  max?: number;
  /**
   * Said to a screen reader, and shown on hover.
   *
   * Required: "separator" is what the role already announces, and a
   * page with two of them needs to say which is which.
   */
  label: string;
  /**
   * The width changed. Called on release and on each keypress, not
   * on every pointer move — a handler that fires sixty times a second
   * is a handler somebody writes a round trip into.
   */
  onResize?: (width: number) => void;
}

export function Split({
  aside,
  children,
  side = "start",
  defaultWidth = 280,
  min = 160,
  max = 640,
  label,
  onResize,
  className,
  ...rest
}: SplitProps) {
  // Scoped to this instance, so two splits on one page do not fight
  // over one custom property.
  const variable = `--ui-split-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const root = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ startX: number; startWidth: number } | null>(null);
  // Mirrored into state only so `aria-valuenow` is right. The layout
  // itself never reads this.
  const [announced, setAnnounced] = useState(defaultWidth);

  const clamp = useCallback(
    (px: number) => {
      const ceiling = Math.min(
        max,
        Math.round(
          (typeof window === "undefined" ? max : window.innerWidth) * MOST_OF_WINDOW,
        ),
      );
      // `min` wins over the ceiling on a very narrow window: a pane
      // below its minimum is unreadable, and unreadable is worse
      // than wide.
      return Math.round(Math.max(min, Math.min(px, Math.max(min, ceiling))));
    },
    [min, max],
  );

  const apply = useCallback(
    (px: number) => {
      const width = clamp(px);
      root.current?.style.setProperty(variable, `${width}px`);
      setAnnounced(width);
      return width;
    },
    [clamp, variable],
  );

  const currentWidth = () =>
    root.current?.firstElementChild?.getBoundingClientRect().width ?? defaultWidth;

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { startX: event.clientX, startWidth: currentWidth() };
    // On the root rather than the strip, so it survives the pointer
    // leaving — which it does on any drag faster than the reflow.
    root.current?.classList.add("ui-split-resizing");
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const from = drag.current;
    if (from === null) return;
    const travelled = event.clientX - from.startX;
    apply(from.startWidth + (side === "start" ? travelled : -travelled));
  };

  const finish = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current === null) return;
    drag.current = null;
    root.current?.classList.remove("ui-split-resizing");
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    onResize?.(clamp(currentWidth()));
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    // A 5px strip is a hard target with a trackpad and an impossible
    // one without a pointer, and this is the only way to move a pane
    // that is otherwise fixed.
    const step = event.shiftKey ? BIG_STEP : STEP;
    const wider = side === "start" ? "ArrowRight" : "ArrowLeft";
    const narrower = side === "start" ? "ArrowLeft" : "ArrowRight";
    let next: number | null = null;
    if (event.key === wider) next = currentWidth() + step;
    else if (event.key === narrower) next = currentWidth() - step;
    else if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    if (next === null) return;
    event.preventDefault();
    onResize?.(apply(next));
  };

  const handle = (
    <div
      className="ui-split-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={label}
      aria-valuenow={announced}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finish}
      onPointerCancel={finish}
      onKeyDown={onKeyDown}
      // Back to where it started. A pane dragged somewhere silly is
      // otherwise a slow drag back to a width nobody remembers.
      onDoubleClick={() => onResize?.(apply(defaultWidth))}
    />
  );

  const panes =
    side === "start"
      ? [
          <div key="aside" className="ui-split-aside">
            {aside}
          </div>,
          handle,
          <div key="main" className="ui-split-main">
            {children}
          </div>,
        ]
      : [
          <div key="main" className="ui-split-main">
            {children}
          </div>,
          handle,
          <div key="aside" className="ui-split-aside">
            {aside}
          </div>,
        ];

  return (
    <div
      {...rest}
      ref={root}
      className={cx("ui-split", side === "end" && "ui-split-end", className)}
      style={{ [variable]: `${defaultWidth}px`, ...rest.style } as React.CSSProperties}
      data-split-var={variable}
    >
      {panes}
    </div>
  );
}
