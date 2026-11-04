import { useCallback, useEffect, useId, useRef, useState } from "react";
import { MenuIcon, type MenuIconName } from "./MenuIcon";

/**
 * A row of actions collapsed behind one button.
 *
 * For the case where the actions are real but secondary: four buttons
 * across a header give each of them the same weight as the page
 * title, and the one that deletes the thing ends up the same size as
 * the one that exports it.
 *
 * Hiding them costs discoverability, so everything that made them
 * usable as buttons has to survive the move — focus, the keyboard,
 * and the names screen readers announce. That is most of what is
 * below.
 */

export interface OverflowAction {
  /** Stable across renders; the label changes on toggles. */
  key: string;
  label: string;
  /** What picking it does. Omitted when `items` makes it a submenu. */
  onSelect?: () => void;
  /** Variants of this action, opened as a submenu rather than run.
   *  Use it when one verb has several objects — export as HTML, as
   *  Markdown, as PDF — so the menu stays a list of things you can
   *  do rather than a list of every combination of them. */
  items?: OverflowAction[];
  /** Destructive — rendered apart from the rest and in the danger colour. */
  danger?: boolean;
  /** A toggle that is currently on. Announced as such, not just coloured. */
  active?: boolean;
  disabled?: boolean;
  /** Why you would pick this. Shown on hover, same as the buttons had. */
  title?: string;
  /** The glyph beside the label.
   *
   *  Optional. A menu of five verbs reads as five equal options, and
   *  picking the wrong one of Archive and Remove costs quite
   *  differently — the shape is recognised before the word is read. */
  icon?: MenuIconName;
}

/** Direct children only — a submenu's own items belong to its list. */
const OWN_ITEMS =
  ":scope > [role='menuitem'], :scope > .overflow-sub-wrap > [role='menuitem']";

function enabledItems(root: HTMLElement | null): HTMLButtonElement[] {
  return Array.from(
    root?.querySelectorAll<HTMLButtonElement>(OWN_ITEMS) ?? [],
  ).filter((el) => !el.disabled);
}

/** Up/down within one list, wrapping. Returns whether it handled the key. */
function moveFocus(root: HTMLElement | null, key: string): boolean {
  if (key !== "ArrowDown" && key !== "ArrowUp") return false;
  const all = enabledItems(root);
  if (!all.length) return false;
  const at = all.indexOf(document.activeElement as HTMLButtonElement);
  const step = key === "ArrowDown" ? 1 : -1;
  // Wrapping, because a menu this short has no scroll to tell you
  // that you have reached the end of it.
  all[(at + step + all.length) % all.length]?.focus();
  return true;
}

export function OverflowMenu({
  actions,
  label = "More actions",
}: {
  actions: OverflowAction[];
  /** What the trigger is called, for anyone who cannot see three dots. */
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const menuId = useId();

  // Closing returns focus to the trigger. Without it a keyboard user
  // who opens the menu and changes their mind is dropped at the top
  // of the document with no way back to where they were.
  const close = useCallback((restoreFocus = true) => {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }, []);

  // Opening moves focus into the menu, which is what makes it a menu
  // rather than a panel that happens to be visible: the next Tab or
  // arrow key lands on an action instead of on whatever follows the
  // trigger in the document.
  useEffect(() => {
    if (open) enabledItems(list.current)[0]?.focus();
  }, [open]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      // Only if no submenu is open — that one owns Escape first, and
      // closes back to here rather than out of the menu entirely.
      if (list.current?.querySelector(".overflow-sub")) return;
      e.stopPropagation();
      close();
      return;
    }
    if (moveFocus(list.current, e.key)) e.preventDefault();
  };

  if (!actions.length) return null;

  const safe = actions.filter((a) => !a.danger);
  const destructive = actions.filter((a) => a.danger);
  const dismiss = () => close(false);

  return (
    <div className="overflow-menu">
      <button
        ref={trigger}
        type="button"
        className="kb-icon-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        title={label}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <circle cx="3" cy="8" r="1.4" fill="currentColor" />
          <circle cx="8" cy="8" r="1.4" fill="currentColor" />
          <circle cx="13" cy="8" r="1.4" fill="currentColor" />
        </svg>
      </button>

      {open && (
        <>
          {/* Outside click closes it — the same backdrop the composer's
              mode picker uses. No focus restore here: the click has
              already put focus wherever the user aimed it, and pulling
              it back to the trigger would undo that. */}
          <div className="overflow-backdrop" onClick={dismiss} />
          <div
            ref={list}
            id={menuId}
            className="overflow-list"
            role="menu"
            aria-label={label}
            onKeyDown={onKeyDown}
          >
            {safe.map((a) => (
              <Entry key={a.key} action={a} onDone={dismiss} />
            ))}
            {/* A separator, so the item that removes the thing is not
                one slip of the wrist below the one that shares it. */}
            {destructive.length > 0 && safe.length > 0 && (
              <div className="overflow-sep" role="separator" />
            )}
            {destructive.map((a) => (
              <Entry key={a.key} action={a} onDone={dismiss} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Entry({
  action,
  onDone,
}: {
  action: OverflowAction;
  onDone: () => void;
}) {
  return action.items?.length ? (
    <Submenu action={action} onDone={onDone} />
  ) : (
    <Item action={action} onDone={onDone} />
  );
}

/**
 * An item that opens its own list beside it.
 *
 * Opens to the *left*. The parent menu is already pinned to the right
 * edge of what it hangs off, so a submenu flying out to the right —
 * the desktop default — would open past the window.
 */
function Submenu({
  action,
  onDone,
}: {
  action: OverflowAction;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const item = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (open) enabledItems(list.current)[0]?.focus();
  }, [open]);

  const back = () => {
    setOpen(false);
    item.current?.focus();
  };

  return (
    <div
      className="overflow-sub-wrap"
      // Hover opens it, the way a desktop menu does. `onMouseLeave`
      // rather than a timer: a submenu that lingers after the pointer
      // has gone covers the items below the one that opened it.
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        ref={item}
        type="button"
        role="menuitem"
        className="popup-item overflow-item overflow-item-parent"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={action.disabled}
        title={action.title}
        onClick={() => setOpen((on) => !on)}
        onKeyDown={(e) => {
          // Right opens and steps in, matching every other menu on
          // the desktop. Enter and Space come free from `button`.
          if (e.key === "ArrowRight" || e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            e.stopPropagation();
            setOpen(true);
          }
        }}
      >
        {action.icon && <MenuIcon name={action.icon} />}
        {action.label}
        <svg
          className="overflow-chevron"
          viewBox="0 0 16 16"
          width="11"
          height="11"
          aria-hidden="true"
        >
          <path
            d="M6 3.5 10.5 8 6 12.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div
          ref={list}
          id={menuId}
          className="overflow-list overflow-sub"
          role="menu"
          aria-label={action.label}
          onKeyDown={(e) => {
            // Escape and Left close *this* list and go back to the
            // item that opened it, rather than dropping the whole
            // menu — one level per keypress, like the trail does.
            if (e.key === "Escape" || e.key === "ArrowLeft") {
              e.preventDefault();
              e.stopPropagation();
              back();
              return;
            }
            if (moveFocus(list.current, e.key)) {
              e.preventDefault();
              e.stopPropagation();
            }
          }}
        >
          {action.items?.map((child) => (
            <Item
              key={child.key}
              action={child}
              onDone={onDone}
              parent={action.label}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Item({
  action,
  onDone,
  parent,
}: {
  action: OverflowAction;
  onDone: () => void;
  /** The submenu this sits under, if any. */
  parent?: string;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      // A toggle is `menuitemradio`-ish, but `aria-pressed` on a
      // menuitem is not read. `aria-current` is: it says "this is the
      // state you are in" rather than leaving the state to the colour.
      aria-current={action.active ? true : undefined}
      // "PDF" alone says nothing once the submenu that gives it
      // meaning has been navigated past. The full name travels with
      // the item.
      aria-label={parent ? `${parent} ${action.label}` : undefined}
      className={[
        "popup-item",
        "overflow-item",
        action.active ? "active" : "",
        action.danger ? "overflow-item-danger" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      disabled={action.disabled}
      title={action.title}
      // Closed without pulling focus back to the trigger, because most
      // of these actions replace the header the trigger lives in —
      // restoring focus to an element that is about to unmount loses
      // it on `document.body`. Where focus goes next belongs to the
      // action. Dismissing without choosing still restores it.
      onClick={() => {
        onDone();
        action.onSelect?.();
      }}
    >
      {action.icon && <MenuIcon name={action.icon} />}
      {action.label}
    </button>
  );
}
