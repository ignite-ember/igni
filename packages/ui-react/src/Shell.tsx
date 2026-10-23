/**
 * The page an extension fills.
 *
 * `.igni-root` set colour, type and `margin: 0`, and nothing about
 * layout — no height, no `box-sizing`, nothing on `html`. So every
 * full-frame extension opened by writing the same six lines of CSS,
 * and wrote them differently. Two panels in one product then scroll
 * differently, which is the kind of difference nobody can name and
 * everybody notices.
 *
 * `min-height: 0` is the reason this is a published component rather
 * than a recipe in a README. A flex child defaults to
 * `min-height: auto`, which refuses to shrink below its content, so
 * a scroll container nested in a flex column grows the page instead
 * of scrolling. It is invisible until the content is long, it is the
 * most-reported layout bug in flexbox, and the fix is one line that
 * reads like it does nothing.
 */
import type { HTMLAttributes, ReactNode } from "react";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export interface AppShellProps extends HTMLAttributes<HTMLDivElement> {
  /** Fixed at the top. A `Toolbar`, usually. */
  header?: ReactNode;
  /** Fixed at the bottom. Status, or a form's actions. */
  footer?: ReactNode;
}

/**
 * Header, body, footer — with exactly one part allowed to grow.
 *
 * The body is a flex column itself, so a `Scroll` placed directly
 * inside it fills what is left over rather than the whole frame.
 */
export function AppShell({
  header,
  footer,
  children,
  className,
  ...rest
}: AppShellProps) {
  return (
    <div {...rest} className={cx("ui-shell", className)}>
      {header === undefined ? null : (
        <div className="ui-shell-header">{header}</div>
      )}
      <div className="ui-shell-body">{children}</div>
      {footer === undefined ? null : (
        <div className="ui-shell-footer">{footer}</div>
      )}
    </div>
  );
}

export interface ScrollProps extends HTMLAttributes<HTMLDivElement> {
  /** Scroll sideways instead. A wide table, not a long list. */
  axis?: "y" | "x";
}

/**
 * The part that scrolls.
 *
 * `overscroll-behavior: contain`, so reaching the end does not chain
 * the scroll to the host page behind the frame — an extension that
 * scrolls igni when its own list bottoms out feels like a bug in
 * igni.
 *
 * Not focusable by default. A scrollable region that holds no
 * focusable children is unreachable by keyboard, so pass
 * `tabIndex={0}` when that is the case; doing it unconditionally
 * would put a stop in the tab order of every list that already has
 * buttons in it.
 */
export function Scroll({ axis = "y", className, ...rest }: ScrollProps) {
  return (
    <div
      {...rest}
      className={cx(axis === "x" ? "ui-scroll-x" : "ui-scroll", className)}
    />
  );
}
