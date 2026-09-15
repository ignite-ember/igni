/**
 * Driving the composer without losing keystrokes.
 *
 * F129. Three specs typed a slash command and pressed Enter on the
 * next line. At three workers that was fine. Once the whole suite ran
 * in one command — eight workers, several starting a Python backend at
 * once — it failed about one run in eight, always as "the drawer never
 * opened", which reads like a slow backend and is not.
 *
 * Instrumenting the WebSocket said what actually happened:
 *
 *     typedText: "cher"
 *     → {"type":"command","text":"/cher"}
 *     ← {"kind":"error","content":"Unknown command: /cher"}
 *
 * ``locator.type()`` sends keystrokes with no delay, and the composer
 * is a contenteditable that React re-renders as early pushes land. Keys
 * pressed into it before it settles go nowhere. Three characters were
 * simply gone and the command that arrived was a different, invalid
 * one. When it worked the round trip took 4–112 ms, so no timeout near
 * the 10 s the assertion allowed was ever the problem.
 *
 * Asserting the text before pressing Enter was not enough either: the
 * composer can be re-rendered in the gap between the assertion and the
 * keypress, and then Enter submits an empty box — measured, as a run
 * with ``typedText: ""`` and not one ``command`` frame on the wire.
 *
 * So the retried unit is the whole thing: type it, send it, and check
 * it took effect. That is what a person does when a keystroke is
 * dropped, and unlike a longer timeout it cannot pass by luck.
 */

import { expect, type Locator, type Page } from "@playwright/test";

/**
 * Run a slash command and wait until it has visibly taken effect.
 *
 * ``settled`` is what the command should produce — the caller names it
 * because the helper retries, and a retry is only safe if the command
 * is idempotent. ``/watcher`` opening a drawer is; anything that
 * appends to the conversation is not, and should not use this.
 */
export async function runSlashCommand(
  page: Page,
  command: `/${string}`,
  settled: Locator,
): Promise<void> {
  const editor = page.locator(".composer-editable");

  await expect(async () => {
    await editor.click();
    // Clear whatever the previous attempt left, or a retry appends to
    // it and sends something longer than the command.
    await editor.press("ControlOrMeta+a");
    await editor.press("Backspace");
    await editor.type(command);
    // The composer renders the leading "/" outside the editable node,
    // so its text is the command without it — confirmed against a real
    // backend, where a successful run reads "watcher" for "/watcher".
    await expect(editor).toHaveText(command.slice(1), { timeout: 2_000 });
    await editor.press("Enter");
    await expect(settled).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 30_000 });
}
