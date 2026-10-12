/**
 * The list that opens when somebody types ``@`` in a comment.
 *
 * A sibling of ``SlashMenu``, drawn separately because it answers a
 * different question. The slash menu offers blocks, which are a fixed
 * list every document shares; this offers people, which comes from
 * the server per document and carries a second fact about each of
 * them — whether they could open the thing being commented on.
 *
 * That second fact is shown **in the list**, not after choosing.
 * Learning that a colleague cannot read the document only once you
 * have finished writing to them is learning it too late to have
 * written something different.
 *
 * Portalled and fixed, like every other popup in this rail: the
 * composer sits inside a scrolling column, and a menu clipped to that
 * column is a menu with one entry visible.
 */
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { MenuIcon } from "../MenuIcon";
import { lockAction, lockLabel, type MentionOptions, type Person } from "./mentions";

/** Roughly how tall the list gets, for deciding above or below. */
const MENU_HEIGHT = 220;

export function MentionMenu({
  people,
  active,
  at,
  loading,
  canGrant,
  busy,
  onChoose,
  onUnlock,
}: {
  people: Person[];
  /** Which row the keyboard is on. */
  active: number;
  /** Where the caret is, in viewport coordinates. */
  at: { left: number; top: number; bottom: number } | null;
  /** The first answer has not arrived yet. */
  loading: boolean;
  /** Whether the person reading this may widen the audience. Decides
   *  whether the padlock grants or asks. */
  canGrant: boolean;
  /** Whose padlock is mid-flight. */
  busy: string;
  onChoose: (person: Person) => void;
  /** Press the padlock: let them in, or ask on their behalf. */
  onUnlock: (person: Person, action: "grant" | "ask") => void;
}) {
  const list = useRef<HTMLUListElement>(null);

  // Keep the highlighted row in view: the keyboard moves the
  // selection, and a selection scrolled out of the list is one
  // somebody is about to press Enter on blind.
  useEffect(() => {
    const row = list.current?.children[active] as HTMLElement | undefined;
    row?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!at) return null;

  const below = window.innerHeight - at.bottom > MENU_HEIGHT;

  return createPortal(
    <ul
      ref={list}
      className="mention-menu"
      role="listbox"
      aria-label="Mention somebody"
      style={{
        left: at.left,
        top: below ? at.bottom + 6 : at.top - 6,
        transform: below ? undefined : "translateY(-100%)",
      }}
    >
      {people.length === 0 ? (
        // Said, rather than an empty box. A menu that vanishes on a
        // typo looks broken; one that says nothing matches looks like
        // a menu. And "looking" is the honest word while the first
        // answer is still in flight — an empty list shown during a
        // round trip reads as "no such person".
        <li className="mention-empty">{loading ? "Looking…" : "Nobody matches that"}</li>
      ) : (
        people.map((person, i) => (
          <li key={person.user_id}>
            <button
              type="button"
              role="option"
              aria-selected={i === active}
              className={i === active ? "mention-item is-active" : "mention-item"}
              // Pointer *down*, not click: clicking moves focus out of
              // the textarea first, and the insertion then lands
              // wherever the caret ended up.
              onPointerDown={(e) => {
                e.preventDefault();
                onChoose(person);
              }}
            >
              <span className="mention-who">
                <span className="mention-line">
                  <span className={person.agent ? "mention-name is-agent" : "mention-name"}>
                    {person.name}
                  </span>
                  {/* Beside the name, not at the far edge of the row.
                      At the edge it reads as a column of statuses to
                      scan; here it is part of reading the name, which
                      is the moment the fact matters.

                      And pressable, because seeing that somebody
                      cannot read the document is immediately followed
                      by wanting to do something about it. What it
                      does depends on who is asking — an owner lets
                      them in, anybody else asks on their behalf —
                      which is a fact about the asker rather than
                      about the wish. */}
                  {!person.has_access && !person.agent && (
                    <Padlock
                      person={person}
                      canGrant={canGrant}
                      busy={busy === person.email}
                      onUnlock={onUnlock}
                    />
                  )}
                </span>
                {/* An address under a colleague's name, because two
                    people called Sam are told apart by nothing else.
                    Under igni, what it is for — it has no address,
                    and "igni / igni" would be a row saying one thing
                    twice. */}
                <span className="mention-email">
                  {person.agent ? "Ask it to change the document" : person.email}
                </span>
              </span>
              {/* Only the people who cannot read it are marked. A tick
                  beside everybody else would make the ordinary case
                  the noisy one — and the question does not apply to
                  igni, which runs on this machine. */}
              {!person.has_access && !person.agent && (
                <span className="mention-locked">
                  {person.pending ? "Asked" : "No access"}
                </span>
              )}
            </button>
          </li>
        ))
      )}
    </ul>,
    document.body,
  );
}

/**
 * The padlock beside a name, which is sometimes a button.
 *
 * Nested inside the row's own button, which is why it is a ``span``
 * with a click handler rather than a ``<button>``: a button inside a
 * button is invalid markup and browsers resolve it by dropping one of
 * them, usually the one you wanted. The keyboard reaches it through
 * the row it is in — the menu is driven by arrows and Enter, and Enter
 * on a row inserts the mention, which is the right default.
 *
 * Stops the press reaching the row. Pressing the padlock means "let
 * them in", not "write this name into my sentence", and doing both
 * would insert a mention every time somebody granted access.
 */
function Padlock({
  person,
  canGrant,
  busy,
  onUnlock,
}: {
  person: Person;
  canGrant: boolean;
  busy: boolean;
  onUnlock: (person: Person, action: "grant" | "ask") => void;
}) {
  const action = lockAction(person, { can_grant: canGrant } as MentionOptions);
  const label = lockLabel(action, !!person.pending);

  if (!action) {
    // Waiting on somebody. The padlock stays — the state it reports
    // has not changed — but there is nothing left to press, and a
    // control that looks pressable and does nothing is worse than one
    // that does not.
    return (
      <span className="mention-lock is-asked" title={label} aria-label={label}>
        <MenuIcon name="lock" />
      </span>
    );
  }

  return (
    <span
      role="button"
      tabIndex={-1}
      className={busy ? "mention-lock is-busy" : "mention-lock"}
      title={label}
      aria-label={label}
      onPointerDown={(e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!busy) onUnlock(person, action);
      }}
    >
      <MenuIcon name="lock" />
    </span>
  );
}
