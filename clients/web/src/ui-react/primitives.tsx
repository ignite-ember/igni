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
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
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

// ── Form controls ───────────────────────────────────────────────────

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {}

export function Select({ className, ...rest }: SelectProps) {
  return <select {...rest} className={cx("ui-select", className)} />;
}

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {}

export function Textarea({ className, ...rest }: TextareaProps) {
  return <textarea {...rest} className={cx("ui-textarea", className)} />;
}

export interface CheckboxProps extends InputHTMLAttributes<HTMLInputElement> {
  label: ReactNode;
}

/**
 * A native checkbox, wrapped in its own label.
 *
 * Wrapped rather than associated by `id`, because an `id` an
 * extension forgets to make unique is a label that checks somebody
 * else's box — and nothing about the page looks wrong when it does.
 */
export function Checkbox({ label, className, ...rest }: CheckboxProps) {
  return (
    <label className={cx("ui-check", className)}>
      <input type="checkbox" {...rest} />
      <span>{label}</span>
    </label>
  );
}

export interface RadioProps extends InputHTMLAttributes<HTMLInputElement> {
  label: ReactNode;
}

export function Radio({ label, className, ...rest }: RadioProps) {
  return (
    <label className={cx("ui-check", className)}>
      <input type="radio" {...rest} />
      <span>{label}</span>
    </label>
  );
}

export interface SwitchProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "onChange"
> {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** Required: a switch with no name is unusable without sight. */
  label: string;
}

/**
 * A switch, which is a button rather than a checkbox.
 *
 * The difference is not cosmetic. A checkbox is a value you submit; a
 * switch takes effect the moment it moves. `role="switch"` is what
 * tells a screen reader which of those it is looking at.
 */
export function Switch({
  checked,
  onChange,
  label,
  className,
  ...rest
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      {...rest}
      className={cx("ui-switch", className)}
    />
  );
}

/**
 * A label, a control, and room for the reason it is wrong.
 *
 * The error slot is the part that gets left out, and a form that
 * rejects input without saying why is the one people abandon. When
 * `error` is set it is wired to the control through
 * `aria-describedby`, so it is announced rather than merely shown.
 */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  className,
  ...rest
}: HTMLAttributes<HTMLDivElement> & {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  /** The id of the control, so the label points at it. */
  htmlFor?: string;
}) {
  const describedBy = htmlFor ? `${htmlFor}-note` : undefined;
  return (
    <div {...rest} className={cx("ui-field", className)}>
      <label className="ui-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? (
        <span className="ui-error" id={describedBy} role="alert">
          {error}
        </span>
      ) : hint ? (
        <span className="ui-hint" id={describedBy}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

// ── Feedback ────────────────────────────────────────────────────────

/** Work whose duration is unknown. `Skeleton` is for known shapes. */
export function Spinner({
  className,
  ...rest
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      {...rest}
      role="status"
      aria-live="polite"
      className={cx("ui-spinner", className)}
    />
  );
}

export interface ProgressProps extends HTMLAttributes<HTMLDivElement> {
  /** 0–1. Omit for indeterminate, which slides rather than sits. */
  value?: number;
  label: string;
}

export function Progress({ value, label, className, ...rest }: ProgressProps) {
  const determinate = typeof value === "number";
  const pct = determinate ? Math.max(0, Math.min(1, value)) * 100 : undefined;
  return (
    <div
      {...rest}
      className={cx("ui-progress", className)}
      role="progressbar"
      aria-label={label}
      aria-valuenow={determinate ? Math.round(pct as number) : undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      data-indeterminate={!determinate}
    >
      <div
        className="ui-progress-fill"
        style={determinate ? { width: `${pct}%` } : undefined}
      />
    </div>
  );
}

// ── Tabs ────────────────────────────────────────────────────────────

export interface TabsProps extends Omit<
  HTMLAttributes<HTMLDivElement>,
  "onChange"
> {
  tabs: { id: string; label: ReactNode }[];
  active: string;
  onChange: (id: string) => void;
  label: string;
}

/**
 * Tabs with the arrow keys wired, because a tab strip that only
 * responds to clicks is a tab strip half the people cannot use.
 */
export function Tabs({
  tabs,
  active,
  onChange,
  label,
  className,
  ...rest
}: TabsProps) {
  const onKeyDown = (event: React.KeyboardEvent) => {
    const step =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const i = tabs.findIndex((t) => t.id === active);
    onChange(tabs[(i + step + tabs.length) % tabs.length].id);
  };
  return (
    <div
      {...rest}
      role="tablist"
      aria-label={label}
      className={cx("ui-tabs", className)}
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={tab.id === active}
          tabIndex={tab.id === active ? 0 : -1}
          className="ui-tab"
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

// ── Text ────────────────────────────────────────────────────────────

export function Divider({ className, ...rest }: HTMLAttributes<HTMLHRElement>) {
  return <hr {...rest} className={cx("ui-divider", className)} />;
}

export interface HeadingProps extends HTMLAttributes<HTMLHeadingElement> {
  /** The outline level. Also picks the size, so they cannot disagree. */
  level?: 1 | 2 | 3 | 4;
}

const HEADING_SIZE = {
  1: "var(--text-2xl)",
  2: "var(--text-xl)",
  3: "var(--text-lg)",
  4: "var(--text-sm)",
} as const;

export function Heading({
  level = 2,
  className,
  style,
  ...rest
}: HeadingProps) {
  const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
  return (
    <Tag
      {...rest}
      className={cx("ui-heading", className)}
      style={{ fontSize: HEADING_SIZE[level], ...style }}
    />
  );
}

export function Text({
  tone = "default",
  className,
  ...rest
}: HTMLAttributes<HTMLParagraphElement> & {
  tone?: "default" | "muted" | "faint";
}) {
  return <p {...rest} data-tone={tone} className={cx("ui-text", className)} />;
}

export function Code({ className, ...rest }: HTMLAttributes<HTMLElement>) {
  return <code {...rest} className={cx("ui-code", className)} />;
}
