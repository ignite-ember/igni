/**
 * Who this document is shared with, and who has it open.
 *
 * One row of faces under the title, and the two facts are drawn on
 * the same faces rather than in two lists. Being shared with somebody
 * and being in the room with them are different things, but they are
 * different things *about the same person* — split into "shared with"
 * and "here now" the reader has to match names across two rows to
 * work out that Sam, who is in both, is one colleague.
 *
 * So: the roster is the row, and presence is a lit ring on it.
 * Somebody present who is not on the roster — which happens whenever
 * a document is shared with a whole domain — is appended, because the
 * question "who is reading this right now" has to be answered
 * completely or it is worse than not asked.
 */
import { useMemo } from "react";

import { Tooltip } from "@igni/ui-react";

/** One person the document names. */
export interface RosterPerson {
  identity: string;
  user_id: string;
  email: string;
  name: string;
  /** ``owner``, ``writer`` or ``reader``. */
  role: string;
}

/** One person with the document open. */
export interface Occupant {
  user_id: string;
  name: string;
  email: string;
  colour: string;
  editing: boolean;
}

export interface Roster {
  people: RosterPerson[];
  /** ``private``, ``users``, ``domain`` or ``public``. */
  reach: string;
  domain: string;
  can_manage: boolean;
  /** The link to hand somebody, when it has been shared. */
  url?: string;
}

/** A face, as one or two letters. */
export function initials(name: string, email: string): string {
  const source = (name || email || "?").trim();
  // An address has no words to take initials from — "sam.patel@acme"
  // would give "S" and nothing else useful — so the local part is
  // split on its punctuation first.
  const words = source.includes("@")
    ? source.split("@")[0].split(/[._-]+/).filter(Boolean)
    : source.split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/**
 * What the row says about reach, beyond the faces.
 *
 * Two of the four visibilities name people and two describe a crowd,
 * and a row of three faces on a document the whole company can read
 * is a row that tells the reader the wrong thing. This is the rest of
 * the sentence.
 */
export function reachNote(roster: Roster): string {
  if (roster.reach === "public") return "Anyone with the link";
  if (roster.reach === "domain") {
    return roster.domain ? `Everyone at ${roster.domain}` : "Everyone at your company";
  }
  if (roster.people.length <= 1) return "Only you";
  return "";
}

/** The word under a face, saying what that person may do. */
function roleNote(role: string): string {
  if (role === "owner") return "Owner";
  if (role === "writer") return "Can edit";
  return "Can read and comment";
}

/**
 * Everybody to draw, in the order to draw them.
 *
 * The roster first and in its own order — the owner leads, because
 * whose document it is is the first thing about it. Then anybody
 * present who is not on it, which is how a domain reader appears.
 */
export function faces(
  roster: Roster | null,
  here: Occupant[],
): { key: string; name: string; email: string; role: string; present: Occupant | null }[] {
  const byId = new Map(here.map((one) => [one.user_id, one]));
  const byEmail = new Map(here.map((one) => [one.email.toLowerCase(), one]));
  const shown = (roster?.people ?? []).map((person) => ({
    key: person.identity,
    name: person.name,
    email: person.email,
    role: person.role,
    present: byId.get(person.user_id) ?? byEmail.get(person.email.toLowerCase()) ?? null,
  }));
  const named = new Set(shown.map((one) => one.email.toLowerCase()));
  const uninvited = here
    .filter((one) => !named.has(one.email.toLowerCase()))
    .map((one) => ({
      key: one.user_id,
      name: one.name,
      email: one.email,
      // Not on the roster, so there is no grant to describe — they
      // are here because of how the document is shared, which the
      // reach note beside them already says.
      role: "",
      present: one,
    }));
  return [...shown, ...uninvited];
}

export function DocumentPeople({
  roster,
  here,
  onManage,
}: {
  roster: Roster | null;
  here: Occupant[];
  /** Open the sharing controls. Given where the control sits, so the
   *  popover can hang off it. Absent for anybody who may not. */
  onManage?: (at: { left: number; right: number; top: number; bottom: number }) => void;
}) {
  const row = useMemo(() => faces(roster, here), [roster, here]);
  const note = roster ? reachNote(roster) : "";

  // Nothing at all on a document that is nobody's business but yours
  // and that nobody else is in. A row of one face saying "Only you"
  // under every private draft is furniture.
  if (!roster || (row.length <= 1 && here.length <= 1 && roster.reach === "private")) {
    return null;
  }

  return (
    <div className="doc-people" aria-label="Who this document is shared with">
      <ul className="doc-faces">
        {row.map((one) => (
          <li key={one.key}>
            <Tooltip
              text={
                one.present
                  ? `${one.name} — ${one.present.editing ? "editing now" : "reading now"}`
                  : `${one.name}${one.role ? ` — ${roleNote(one.role)}` : ""}`
              }
            >
              <span
                className={[
                  "doc-face",
                  one.present ? "is-here" : "",
                  one.present?.editing ? "is-editing" : "",
                  one.role === "owner" ? "is-owner" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                // The ring, in the colour the server gave them — the
                // same colour their cursor carries, so a face and a
                // caret in the text are recognisably one person.
                style={one.present ? { ["--face-ring" as string]: one.present.colour } : undefined}
              >
                {initials(one.name, one.email)}
              </span>
            </Tooltip>
          </li>
        ))}
      </ul>
      {/* The sentence about reach *is* the control. "Anyone with the
          link" is already the answer to "who can see this", so
          pressing it to change that reads as one thing rather than as
          a statement with a separate Share button next to it saying
          the same thing again. */}
      {note &&
        (onManage ? (
          <button
            type="button"
            className="doc-people-note is-pressable"
            title="Change who can see this"
            onClick={(e) => {
              const box = e.currentTarget.getBoundingClientRect();
              onManage({ left: box.left, right: box.right, top: box.top, bottom: box.bottom });
            }}
          >
            {note}
          </button>
        ) : (
          <span className="doc-people-note">{note}</span>
        ))}
    </div>
  );
}
