/**
 * The list that opens when somebody types ``/``.
 *
 * Drawn here rather than inside the editor extension, so it is a
 * React component with the app's own styling instead of a second
 * rendering system living inside ProseMirror.
 *
 * Portalled and fixed, for the same reason every other popup in this
 * panel is: the editor sits in a column between two docked panels,
 * and a menu clipped to that column is a menu with two items visible.
 */
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import type { SlashState } from "./slashCommands";

/** Roughly how tall the list gets, for deciding above or below. */
const MENU_HEIGHT = 240;

export function SlashMenu({ state }: { state: SlashState | null }) {
  const list = useRef<HTMLUListElement>(null);

  // Keep the highlighted row in view. The keyboard moves the
  // selection, and a selection that has scrolled out of the list is
  // a selection somebody is about to press Enter on blind.
  useEffect(() => {
    if (!state) return;
    const row = list.current?.children[state.active] as HTMLElement | undefined;
    row?.scrollIntoView({ block: "nearest" });
  }, [state]);

  if (!state || !state.at) return null;

  const room = window.innerHeight - state.at.bottom;
  const below = room > MENU_HEIGHT;

  return createPortal(
    <ul
      ref={list}
      className="slash-menu"
      role="listbox"
      aria-label="Insert a block"
      style={{
        left: state.at.left,
        top: below ? state.at.bottom + 6 : state.at.top - 6,
        transform: below ? undefined : "translateY(-100%)",
      }}
    >
      {state.items.length === 0 ? (
        // Said, rather than an empty box. A menu that vanishes on a
        // typo looks like a broken menu; one that says nothing
        // matches looks like a menu.
        <li className="slash-empty">Nothing matches that</li>
      ) : (
        state.items.map((item, i) => (
          <li key={item.label}>
            <button
              type="button"
              role="option"
              aria-selected={i === state.active}
              className={i === state.active ? "slash-item is-active" : "slash-item"}
              // Pointer *down*, not click: clicking moves focus out
              // of the editor first, and the insertion then lands
              // wherever the selection ended up.
              onPointerDown={(e) => {
                e.preventDefault();
                state.choose(i);
              }}
            >
              <span className="slash-label">{item.label}</span>
              {item.detail && <span className="slash-detail">{item.detail}</span>}
            </button>
          </li>
        ))
      )}
    </ul>,
    document.body,
  );
}
