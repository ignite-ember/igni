/**
 * Typed wrappers over the CSS primitives.
 *
 * The classes already existed — `btn`, `ui-card`, `ui-row`,
 * `ui-badge` — and an extension could write them by hand. These
 * exist because writing them by hand is not an API: there is no
 * autocomplete, no way to learn the variant names short of reading
 * `theme.css`, and nothing stops `class="btn btn-primery"` from
 * shipping as an invisible bug.
 *
 * Every one of these is a thin mapping from props to class names.
 * They hold no state and make no decisions. That is deliberate: the
 * appearance lives in CSS where the app and the extension share it,
 * and a wrapper that started making layout choices would be a second
 * place for the design to live.
 *
 * `Dialog` is the exception and says so at its own definition — a
 * dialog is behaviour, not just a box.
 *
 * **Under the compatibility floor.** A prop here is a promise; see
 * EXTENSIBILITY.md §8.
 */

import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
} from "react";
import { useCallback, useEffect, useRef } from "react";

/** Joins class names, skipping anything falsy. */
function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// ── Controls ────────────────────────────────────────────────────────

export type ButtonVariant = "default" | "primary" | "danger" | "warn" | "quiet";
export type ButtonSize = "md" | "sm" | "xs";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({
  variant = "default",
  size = "md",
  className,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      {...rest}
      className={cx(
        "btn",
        variant !== "default" && `btn-${variant}`,
        size !== "md" && `btn-${size}`,
        className,
      )}
    />
  );
}

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {}

export function Input({ className, ...rest }: InputProps) {
  return <input {...rest} className={cx("panel-input", className)} />;
}

export type Tone = "neutral" | "success" | "warning" | "danger";

export interface ChipProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

export function Chip({ tone = "neutral", className, ...rest }: ChipProps) {
  return <span {...rest} data-tone={tone} className={cx("chip", className)} />;
}

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

export function Badge({ tone = "neutral", className, ...rest }: BadgeProps) {
  return (
    <span {...rest} data-tone={tone} className={cx("ui-badge", className)} />
  );
}

// ── Layout ──────────────────────────────────────────────────────────

/** A horizontal run of things, vertically centred. */
export function Row({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cx("ui-row", className)} />;
}

/** A horizontal run of buttons. Separate from `Row` because it is
 *  what a reader looks for at the foot of a panel. */
export function Actions({
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cx("ui-actions", className)} />;
}

/** A vertical stack. */
export function List({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cx("ui-list", className)} />;
}

export function Toolbar({
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cx("ui-toolbar", className)} />;
}

// ── Containers ──────────────────────────────────────────────────────

/** A surface: background, border, radius, padding. */
export function Card({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cx("ui-card", className)} />;
}

/** One selectable row inside a list. Renders a button, because
 *  something clickable that is not a button is a keyboard trap. */
export function Item({
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" {...rest} className={cx("ui-item", className)} />
  );
}

export function Table({
  className,
  ...rest
}: HTMLAttributes<HTMLTableElement>) {
  return <table {...rest} className={cx("ui-table", className)} />;
}

/**
 * What to show where there is nothing.
 *
 * Its own component rather than a paragraph with a class, because an
 * empty state that says only "nothing here" is the commonest way a
 * panel wastes the one moment it had the reader's attention. The
 * `action` slot is a nudge to say what to do next.
 */
export function Empty({
  children,
  action,
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { action?: ReactNode }) {
  return (
    <div {...rest} className={cx("ui-empty", className)}>
      {children}
      {action}
    </div>
  );
}

// ── Dialog ──────────────────────────────────────────────────────────

export interface DialogProps {
  title: ReactNode;
  /** Shown under the title. A sentence, not a paragraph. */
  sub?: ReactNode;
  children?: ReactNode;
  /** The buttons. Rendered in an `Actions` row at the foot. */
  actions?: ReactNode;
  /** Escape, or a click on the backdrop. */
  onClose: () => void;
}

/**
 * The one primitive here that is behaviour rather than a box.
 *
 * A dialog has to trap focus, close on Escape, restore focus to
 * whatever opened it, and not let a click inside the panel count as a
 * click on the backdrop. Every extension that built its own would get
 * some of that wrong, and the ones they get wrong are the ones only
 * keyboard and screen-reader users notice.
 */
export function Dialog({
  title,
  sub,
  children,
  actions,
  onClose,
}: DialogProps) {
  const panel = useRef<HTMLDivElement | null>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    opener.current = document.activeElement;
    // Focus the panel itself rather than the first control: reading
    // the title before the buttons is the point of having one.
    panel.current?.focus();
    return () => {
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = panel.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  return (
    <div className="overlay" onMouseDown={onClose}>
      <div
        ref={panel}
        className="dialog"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        // Without this a drag that starts inside and ends outside
        // closes the dialog, which eats a selection.
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 className="dialog-title">{title}</h2>
        {sub ? <p className="dialog-sub">{sub}</p> : null}
        {children}
        {actions ? (
          <Actions className="dialog-actions">{actions}</Actions>
        ) : null}
      </div>
    </div>
  );
}
