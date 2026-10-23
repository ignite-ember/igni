/**
 * Choosing which colleagues can open a document.
 *
 * It was a textarea. You typed addresses separated by commas, from
 * memory, and found out whether you had spelled them right by sharing
 * the document and waiting for somebody to say they could not see it.
 * Everything wrong with that is the same thing: the people who could
 * be picked were already known, and the control did not say so.
 *
 * So: the directory, searchable, with a tick beside everybody who
 * will be able to read this. The tick is the whole interface — it is
 * a list of people and a yes-or-no about each of them, which is
 * exactly what an audience is.
 *
 * **The tick means "will have access", not "does".** Pressing one is
 * the only way to change the answer, so it has to change immediately;
 * a tick that waited for Publish would look broken for the second
 * between the click and the save. Whoever is already in the audience
 * starts ticked, so the list opens showing the truth and every change
 * to it is the reader's own.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { IgniClient } from "../../protocol/client";
import { MenuIcon } from "@igni/ui-react";
import { initials } from "./DocumentPeople";

export interface Colleague {
  user_id: string;
  email: string;
  name: string;
}

/** How long to wait after a keystroke before asking the server. */
const LOOKUP_DEBOUNCE = 140;

/**
 * Everybody worth showing, in the order to show them.
 *
 * The chosen first, then the rest of the directory. Somebody who has
 * just been picked must not sort away under a search that no longer
 * matches them — the list is the audience *and* the way to change it,
 * and a name that vanishes on being chosen reads as the choice not
 * having taken.
 *
 * Addresses that match nobody in the directory are still shown, at
 * the end. An owner may share with a colleague before that colleague
 * has an account, and a picker that hid them would show an audience
 * missing the people it actually names.
 */
export function ordered(
  directory: Colleague[],
  chosen: string[],
  query: string,
): Colleague[] {
  const known = new Map(directory.map((one) => [one.email.toLowerCase(), one]));
  const asked = query.trim().toLowerCase();

  // Only when nothing is being searched. While a search is running
  // the server's answer *is* the list, and filtering the chosen here
  // as well meant two matching rules in one list: the server matches
  // a prefix, this matched anywhere inside, so "am" showed nothing
  // from the directory and kept a ticked "sam@acme.test" on screen.
  const first = (asked ? [] : chosen)
    .map(
      (identity) =>
        known.get(identity.toLowerCase()) ?? {
          user_id: "",
          email: identity,
          // No account, so no name to show. The address is who they
          // are as far as anybody here knows.
          name: identity,
        },
    );
  const rest = directory.filter(
    (one) => !first.some((chose) => chose.email.toLowerCase() === one.email.toLowerCase()),
  );
  return [...first, ...rest];
}

/** Add or remove one person, leaving the rest alone. */
export function toggled(chosen: string[], email: string): string[] {
  const at = chosen.findIndex((one) => one.toLowerCase() === email.toLowerCase());
  return at < 0 ? [...chosen, email] : chosen.filter((_, i) => i !== at);
}

/** Whether an address is in the audience being built. */
export function isChosen(chosen: string[], email: string): boolean {
  return chosen.some((one) => one.toLowerCase() === email.toLowerCase());
}

/**
 * Whether what has been typed could be an address nobody listed.
 *
 * The escape hatch. The directory is colleagues; somebody may want to
 * share with a contractor who has an account and no org, or with a
 * new hire who has neither. Offered only once it looks like an
 * address, so the row does not appear under every half-typed name.
 */
export function offerableAddress(
  query: string,
  shown: Colleague[],
  chosen: string[] = [],
): string {
  const typed = query.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(typed)) return "";
  // Not for somebody already in the audience. They may be absent
  // from the list — searching narrows to the directory, and an
  // address with no account is in neither — and offering to add
  // somebody who is already added reads as the first add not having
  // worked.
  if (chosen.some((one) => one.toLowerCase() === typed)) return "";
  return shown.some((one) => one.email.toLowerCase() === typed) ? "" : typed;
}

export function PeoplePicker({
  client,
  chosen,
  onChange,
}: {
  client: IgniClient;
  /** The audience as it stands. Addresses, because that is what the
   *  access rule matches on. */
  chosen: string[];
  onChange: (next: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [directory, setDirectory] = useState<Colleague[]>([]);
  const [looking, setLooking] = useState(true);
  const list = useRef<HTMLUListElement>(null);

  useEffect(() => {
    let dropped = false;
    setLooking(true);
    const timer = window.setTimeout(() => {
      void client
        .rpc<{ people: Colleague[] }>("artifact_colleagues", { query })
        .then((answer) => {
          if (!dropped) setDirectory(answer?.people ?? []);
        })
        .catch(() => {
          // A picker with nothing in it says "nobody to pick" on its
          // own. An error drawn over a sharing dialog reads as the
          // sharing having failed.
          if (!dropped) setDirectory([]);
        })
        .finally(() => {
          if (!dropped) setLooking(false);
        });
    }, LOOKUP_DEBOUNCE);
    return () => {
      dropped = true;
      window.clearTimeout(timer);
    };
  }, [client, query]);

  const shown = useMemo(
    () => ordered(directory, chosen, query),
    [directory, chosen, query],
  );
  const typedAddress = useMemo(
    () => offerableAddress(query, shown, chosen),
    [query, shown, chosen],
  );

  const pick = useCallback(
    (email: string) => {
      onChange(toggled(chosen, email));
    },
    [chosen, onChange],
  );

  return (
    <div className="kb-pick">
      <input
        type="text"
        className="kb-pick-search"
        value={query}
        placeholder="Search your colleagues, or type an address"
        aria-label="Search people"
        onChange={(e) => setQuery(e.target.value)}
      />

      <ul className="kb-pick-list" ref={list} role="listbox" aria-multiselectable="true">
        {shown.map((person) => {
          const on = isChosen(chosen, person.email);
          return (
            <li key={person.email}>
              <button
                type="button"
                role="option"
                aria-selected={on}
                className={on ? "kb-pick-row is-on" : "kb-pick-row"}
                onClick={() => pick(person.email)}
              >
                <span className="kb-pick-face">{initials(person.name, person.email)}</span>
                <span className="kb-pick-who">
                  <span className="kb-pick-name">{person.name}</span>
                  {person.name !== person.email && (
                    <span className="kb-pick-email">{person.email}</span>
                  )}
                </span>
                {/* The tick is the whole interface: a list of people
                    and a yes-or-no about each. Drawn only when the
                    answer is yes — an empty box beside everybody else
                    would make the ordinary state the loud one. */}
                {on && (
                  <span className="kb-pick-tick" aria-hidden="true">
                    <MenuIcon name="resolve" />
                  </span>
                )}
              </button>
            </li>
          );
        })}

        {typedAddress && (
          <li>
            <button
              type="button"
              className="kb-pick-row kb-pick-new"
              onClick={() => {
                pick(typedAddress);
                setQuery("");
              }}
            >
              <span className="kb-pick-face">+</span>
              <span className="kb-pick-who">
                <span className="kb-pick-name">Share with {typedAddress}</span>
                <span className="kb-pick-email">Not in your organisation</span>
              </span>
            </button>
          </li>
        )}

        {shown.length === 0 && !typedAddress && (
          <li className="kb-pick-empty">
            {looking
              ? "Looking…"
              : query.trim()
                ? "Nobody matches that. Type a whole address to share with somebody outside."
                : "Nobody to pick yet."}
          </li>
        )}
      </ul>

      <p className="kb-pick-count">
        {chosen.length === 0
          ? "Nobody yet — pick at least one person"
          : `${chosen.length} ${chosen.length === 1 ? "person" : "people"}`}
      </p>
    </div>
  );
}
