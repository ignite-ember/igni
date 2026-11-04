/**
 * Messages, by the run they belong to.
 *
 * The window's model names a message by its run (`ui.selection.message_id`,
 * `ui.scroll.message_id`, `ui.scroll_to`): the `run_id` an extension sees
 * in `chat.messages` and the run signals. The transcript's items carry
 * their run where they have one — the person's message and the tool
 * cards; the assistant's text sits under the person's message it answers.
 */

import type { ChatItem } from "./model";

function runOf(item: ChatItem): string {
  if ((item.kind === "user" || item.kind === "tool") && item.runId) return item.runId;
  return "";
}

/** The run the item at `index` belongs to: its own, or that of the
 *  nearest item above it that has one. `null` before any run. */
export function runAt(items: readonly ChatItem[], index: number): string | null {
  for (let i = Math.min(index, items.length - 1); i >= 0; i--) {
    const run = runOf(items[i]);
    if (run) return run;
  }
  return null;
}

/** Where a run's messages begin — the person's message, else its first
 *  tool card. `-1` when no item is of that run. */
export function indexOfRun(items: readonly ChatItem[], runId: string): number {
  let fallback = -1;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (runOf(item) !== runId) continue;
    if (item.kind === "user") return i;
    if (fallback < 0) fallback = i;
  }
  return fallback;
}
