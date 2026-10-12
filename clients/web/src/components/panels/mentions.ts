/**
 * Naming somebody in a comment.
 *
 * Two questions, and only the first one is about text. "Who do you
 * mean" is answered here, by reading what has been typed after an
 * ``@``. "Can that person open this document" is answered by the
 * server, because it is a property of how the document is shared and
 * not of anything this machine holds.
 *
 * The second question is the reason the feature exists. Mentioning a
 * colleague on a private document used to be a thing you could do
 * that did nothing at all: they were never told, the question sat
 * there unanswered, and nothing anywhere said why. So the composer
 * asks before the comment is sent rather than after.
 *
 * **What a mention writes is the local part of an address.**
 * ``@sam.patel``, not ``@sam.patel@contractor.test`` — the second is
 * what the access rule matches on, but nobody wants a sentence with
 * two ``@`` in one word. The full address comes back by looking the
 * handle up in the directory the popup was drawn from, which is also
 * where the ambiguity is handled: two people whose addresses start
 * the same way get their full address written instead, because a
 * mention that could mean either of two colleagues is not a mention.
 */

/** One person, as the server describes them. */
export interface Person {
  user_id: string;
  email: string;
  name: string;
  has_access: boolean;
  /** Somebody has already asked for them to be let in. A different
   *  offer from ``has_access``: there is nothing left to press. */
  pending?: boolean;
  /** Not a colleague — the agent. Drawn differently, and never the
   *  subject of an access question. */
  agent?: boolean;
}

/**
 * igni, as an entry in the same list as everybody else.
 *
 * It belongs there because it is the mention that already worked, and
 * the only one anybody had to be told about — the composer carried a
 * line of hint text saying so. A menu that offered every colleague
 * and silently omitted the one name the hint recommended would read
 * as igni not being mentionable at all.
 *
 * It is not in the directory the server answers with, and should not
 * be: that directory is people with accounts, and the question it
 * answers about each of them — can they read this document — is not a
 * question about the agent. It runs on the machine holding the
 * document. So it is added here, client-side, where "who can I
 * address" is being asked, rather than folded into the answer to
 * "who works here".
 */
export const IGNI: Person = {
  user_id: "igni",
  email: "igni",
  name: "igni",
  has_access: true,
  agent: true,
};

/** Whether what has been typed should offer igni. */
export function offersIgni(query: string): boolean {
  return "igni".startsWith(query.trim().toLowerCase());
}

/**
 * The menu's list: igni first, then the colleagues.
 *
 * First because it is the most-used mention in this rail by a wide
 * margin, and because its position should not move as a directory
 * answer arrives underneath it — a list whose top entry changes
 * between one keystroke and the next is a list you cannot press Enter
 * on without looking.
 */
export function withAgent(people: Person[], query: string): Person[] {
  return offersIgni(query) ? [IGNI, ...people] : people;
}

/** What the server answers when asked who could be named here. */
export interface MentionOptions {
  people: Person[];
  can_grant: boolean;
  owner_name: string;
  owner_email: string;
  error: string;
}

/** The ``@`` being typed, and where it starts. */
export interface MentionSpan {
  /** Index of the ``@`` itself. */
  start: number;
  /** What has been typed after it, up to the caret. */
  query: string;
}

/** Characters a handle is made of. Everything else ends it. */
const HANDLE = /^[\w.+-]*$/;

/**
 * The mention under the caret, if the caret is in one.
 *
 * An ``@`` only opens a mention at the start of the text or after
 * whitespace. In the middle of a word it is an address somebody is
 * typing out — "write to sam@acme.test" — and a popup that opened
 * there would fire while somebody wrote down an email address, which
 * is a thing people do in comments constantly.
 */
export function mentionSpan(text: string, caret: number): MentionSpan | null {
  const before = text.slice(0, caret);
  const at = before.lastIndexOf("@");
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(before[at - 1])) return null;
  const query = before.slice(at + 1);
  return HANDLE.test(query) ? { start: at, query } : null;
}

/**
 * Replace the half-typed mention with a chosen one.
 *
 * A trailing space, because a mention is nearly always followed by
 * more sentence and typing it yourself after every name is the kind
 * of small tax that makes a feature feel unfinished.
 */
export function insertMention(
  text: string,
  span: MentionSpan,
  handle: string,
): { text: string; caret: number } {
  const written = `@${handle} `;
  const after = span.start + span.query.length + 1;
  return {
    text: text.slice(0, span.start) + written + text.slice(after),
    caret: span.start + written.length,
  };
}

/**
 * What a mention of this person writes.
 *
 * The local part, unless somebody else in the directory shares it —
 * then the whole address, because two people are two mentions and a
 * handle that names both names neither.
 */
export function handleFor(person: Person, everyone: Person[]): string {
  const local = person.email.split("@")[0];
  const shared = everyone.filter((other) => other.email.split("@")[0] === local);
  return shared.length > 1 ? person.email : local;
}

/** Every finished ``@handle`` in a comment, lowercased. */
export function writtenHandles(text: string): string[] {
  return [
    ...new Set(
      Array.from(text.matchAll(/(?:^|\s)@([\w.+-]+(?:@[\w.-]+)?)/g), (m) => m[1].toLowerCase()),
    ),
  ];
}

/**
 * The people a written comment actually names.
 *
 * Read back out of the text rather than remembered from what was
 * clicked, so that deleting the name deletes the notice under the box
 * along with it. A notice about somebody who is no longer mentioned
 * is worse than none: it asks for a decision about a sentence that
 * has already been rewritten.
 */
export function namedIn(text: string, everyone: Person[]): Person[] {
  const written = new Set(writtenHandles(text));
  return everyone.filter(
    (person) =>
      written.has(person.email.toLowerCase()) ||
      written.has(person.email.split("@")[0].toLowerCase()),
  );
}

/**
 * Handles in the text that nobody known accounts for.
 *
 * The composer's other reason to ask the server. The menu asks about
 * what is being typed *now*; this asks about names already finished —
 * pasted in, typed faster than the menu's pause, or written straight
 * out by somebody who knows the address. Without it, the fastest way
 * to mention a colleague was also the way that told you nothing about
 * whether they would ever see it.
 *
 * ``@igni`` is never one of these. It is the agent, it is in every
 * other sentence in this rail, and asking the directory about it
 * would be a request per comment that can only come back empty.
 */
export function unknownHandles(text: string, everyone: Person[]): string[] {
  const known = new Set(
    everyone.flatMap((person) => [
      person.email.toLowerCase(),
      person.email.split("@")[0].toLowerCase(),
    ]),
  );
  return writtenHandles(text).filter((handle) => handle !== "igni" && !known.has(handle));
}

/**
 * The ones named who could not open the document.
 *
 * The whole output of this module, as far as the composer is
 * concerned: everybody about to be talked to who would never see it.
 */
export function cannotRead(text: string, everyone: Person[]): Person[] {
  return namedIn(text, everyone).filter((person) => !person.has_access);
}

/**
 * What to say about somebody who cannot read the document.
 *
 * Phrased as a fact and a way out, never as a warning — mentioning
 * somebody who lacks access is a perfectly reasonable thing to do,
 * and it is usually the first half of giving it to them.
 */
export function accessNotice(
  person: Person,
  options: Pick<MentionOptions, "can_grant" | "owner_name">,
): string {
  const who = person.name || person.email;
  if (person.pending) {
    const owner = options.owner_name || "its owner";
    return `${who} can’t open this document yet — ${owner} has been asked.`;
  }
  if (options.can_grant) return `${who} can’t open this document yet.`;
  const owner = options.owner_name || "its owner";
  return `${who} can’t open this document. Only ${owner} can change that.`;
}

/**
 * What pressing the padlock beside a name does.
 *
 * An owner lets them in; anybody else asks on their behalf. Two
 * different acts behind one control, because from where the person
 * typing sits they are the same intention — "this colleague should
 * be able to read what I am writing to them" — and which of the two
 * it turns into is a fact about the asker, not about the wish.
 *
 * ``null`` when there is nothing left to do, which is what makes the
 * padlock stop being a button once somebody is waiting.
 */
export function lockAction(
  person: Person,
  options: Pick<MentionOptions, "can_grant">,
): "grant" | "ask" | null {
  if (person.has_access || person.agent || person.pending) return null;
  return options.can_grant ? "grant" : "ask";
}

/** What the control says it will do. */
export function lockLabel(action: "grant" | "ask" | null, pending: boolean): string {
  if (pending) return "Access requested";
  if (action === "grant") return "Give access";
  if (action === "ask") return "Request access for them";
  return "";
}
