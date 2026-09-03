/**
 * Counting things without saying "1 message(s)".
 *
 * F135. The crash-recovery notice read
 * `${pending.length} message(s) above were interrupted` — the one
 * sentence a user sees after the backend has died, hedging on whether
 * it was one message or several. `(s)` is never right, only never
 * wrong.
 *
 * The same defect and the same helper as the portal's F132: two
 * clients, one habit. English plurals are irregular often enough that
 * the caller has to be able to say so, so the plural form is an
 * argument; the default is `+ 's'` because most of these nouns are
 * regular.
 */

/** `3` + `message` → `"3 messages"`. `1` + `message` → `"1 message"`. */
export function countOf(n: number, singular: string, plural = `${singular}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? singular : plural}`;
}

/** The noun alone, for when the number is rendered separately. */
export function pluralise(n: number, singular: string, plural = `${singular}s`): string {
  return n === 1 ? singular : plural;
}
