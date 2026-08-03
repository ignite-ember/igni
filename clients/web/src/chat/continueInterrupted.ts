/** Helper: build a "continue from partial" prompt.
 *
 * When the user submits a new message while there's an interrupted
 * assistant bubble still in the chat, the agent has no native
 * memory of what it was saying — the partial text is just
 * visible HTML in the chat, not part of any subsequent request.
 *
 * The user's mental model is "continue" — type any message and
 * the agent should pick up where it left off. The implementation:
 * scan items for the most recent interrupted assistant bubble;
 * if found, prepend a fenced context block to the new user
 * message so the agent sees the partial before responding.
 *
 * Returns the user's text unchanged when there's no interrupted
 * bubble — the helper is a no-op for the normal path.
 *
 * Also returns ``{clearIndex}`` alongside the augmented text so
 * the caller can clear the ``interrupted`` flag on the source
 * bubble once the partial has been consumed (otherwise the same
 * partial would be prepended to every subsequent message until
 * the user hits Discard).
 */

import type { ChatItem } from "./model";

export interface ContinuedPrompt {
  /** The text to send to the agent (potentially augmented with
   * the partial as context). */
  text: string;
  /** Index in ``items`` of the assistant bubble whose
   * ``interrupted`` flag was consumed. ``-1`` when no partial
   * was found (the helper was a no-op). */
  clearIndex: number;
}

export function buildContinuedPrompt(
  items: readonly ChatItem[],
  userText: string,
): ContinuedPrompt {
  let partial: string | null = null;
  let idx = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "assistant" && it.interrupted) {
      partial = it.text;
      idx = i;
      break;
    }
  }
  if (partial === null) {
    return { text: userText, clearIndex: -1 };
  }

  // The augmented message gives the agent enough context to pick
  // up where it left off. Wrapping the partial in a 5-backtick
  // fenced block (longer than any 3-backtick sequence the partial
  // could contain) prevents any markdown in the partial
  // (headings, bullet lists, code fences) from being mis-rendered
  // as if it were the new prompt. ``JSON.stringify`` escapes the
  // user's text — quotes become ``\"``, newlines become ``\n`` —
  // so the agent sees the exact characters the user typed.
  // The trailing ``The user then typed:`` line keeps the user's
  // intent visible without losing the surrounding context.
  const text = [
    "Continue from where you left off.",
    "",
    "Your previous partial response was:",
    "`````",
    partial,
    "`````",
    "",
    `The user then typed: ${JSON.stringify(userText)}`,
  ].join("\n");

  return { text, clearIndex: idx };
}
