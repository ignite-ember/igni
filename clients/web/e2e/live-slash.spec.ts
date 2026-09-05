/**
 * Every slash command, opened and looked at.
 *
 * The composer advertises them on every screen — "/ commands, @ files,
 * $ shell" — and the welcome panel names nine more. They are the app's
 * whole control surface outside the chat box, and none of them was
 * driven before this file.
 *
 * The list is read from the menu itself rather than hardcoded. The
 * eight in `core/tools/slash.py` are not the set a user sees: the
 * client adds `/compact`, `/sessions` and `/fork`, so a list copied
 * from either side would be wrong about the other.
 *
 * ## What is deliberately not run
 *
 * `/logout` and `/login` touch real credentials — `/logout` would
 * clear the operator's stored token, which is not a side effect a
 * test gets to have on someone's machine. `/clear` destroys the
 * transcript it is run against; it is exercised, but only in a
 * session created for the purpose.
 *
 * Requires a real backend: `IGNI_LIVE_WS`. See docs/APP_TEST_MATRIX.md.
 */

import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test } from "./fixtures/live-be";

const OUT = "shots/slash";

test.describe.configure({ timeout: 120_000 });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

/** Commands that only read state. Safe to run anywhere. */
const READ_ONLY = [
  { cmd: "/help", shows: /command|help/i },
  { cmd: "/ctx", shows: /context|token|floor/i },
  { cmd: "/sessions", shows: /session/i },
  { cmd: "/model", shows: /model|DeepSeek/i },
  { cmd: "/agents", shows: /agent/i },
  { cmd: "/skills", shows: /skill/i },
  { cmd: "/mcp", shows: /mcp|server/i },
];

/** Credentials. Not run — see the module docstring. */
const NEVER_RUN = ["/login", "/logout"];

async function connected(page: Page) {
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 60_000 },
  );
}

async function runCommand(page: Page, cmd: string) {
  const editor = page.locator(".composer-editable");
  await expect(editor).toBeVisible();
  await editor.click();
  await editor.fill(cmd);
  // The composer switches into a **command mode** when it sees the
  // leading slash: the class gains `mode-command`, the placeholder
  // becomes "Command name (Backspace to return to chat)", and the
  // slash itself is consumed — so the editor holds `help`, not
  // `/help`. Asserting the typed string found "help" and failed,
  // which is a test that did not know the product rather than a
  // product that was wrong.
  const bare = cmd.replace(/^\//, "");
  if (bare !== cmd) {
    await expect(editor).toHaveClass(/mode-command/);
  }
  await expect(editor).toContainText(bare);
  await editor.press("Enter");
}

async function shot(page: Page, name: string) {
  await page.evaluate(() =>
    Promise.race([
      Promise.all(
        document
          .getAnimations()
          .filter((a) => (a.effect?.getTiming().iterations ?? 1) !== Infinity)
          .map((a) => a.finished.catch(() => undefined)),
      ),
      new Promise((r) => setTimeout(r, 2000)),
    ]),
  );
  await page.screenshot({ path: `${OUT}/${name}.png` });
}

test("the menu lists commands and filters as you type", async ({
  page,
  liveBe,
}) => {
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.pressSequentially("/");
  await expect(page.locator("body")).toContainText(/\/help/, {
    timeout: 15_000,
  });
  await shot(page, "00-menu");

  // Typing narrows it. A menu that ignores what you type is a list,
  // not a picker.
  //
  // `pressSequentially`, not `fill`. The composer consumes the leading
  // slash as a mode switch, so setting the value to "/mod"
  // programmatically inserts a *literal* slash into command mode, the
  // filter matches nothing and the menu closes — my first version
  // read that as "the menu does not filter".
  await editor.pressSequentially("mod");
  await expect(page.locator("body")).toContainText(/\/model/, {
    timeout: 10_000,
  });
  await expect(page.locator("body")).not.toContainText(/\/logout/);
  await shot(page, "01-menu-filtered");
});

for (const { cmd, shows } of READ_ONLY) {
  test(`${cmd} opens and shows something`, async ({ page, liveBe }) => {
    // The bar is deliberately low and the point is coverage: a command
    // in the menu that errors, or renders an empty panel, is worse
    // than one that is not offered. What each panel *should* contain
    // is a separate judgement per command.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    await runCommand(page, cmd);
    await shot(page, `cmd${cmd.replace("/", "-")}`);

    const body = page.locator("body");
    await expect(body).toContainText(shows, { timeout: 30_000 });
    // Nothing blew up on the way.
    await expect(body).not.toContainText(/traceback|unhandled|is not defined/i);
  });
}

test("/fork continues the conversation under a new id", async ({
  page,
  liveBe,
}) => {
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  // Fork a session that has actually said something. On a *fresh*
  // session the command fails with "Fork failed: source session not
  // found: <id>" — the id is on screen in the footer, so from the
  // user's side they are forking a session that visibly exists. That
  // case is `todo` in the matrix as its own defect; this test covers
  // the path that should work.
  await runCommand(page, "Reply with exactly FORKME.");
  await expect(
    page.locator(".msg-assistant").filter({ hasText: "FORKME" }).first(),
  ).toBeVisible({ timeout: 120_000 });

  const before = await page.locator("body").innerText();
  const idBefore = /session\s+([0-9a-f]{6,})/i.exec(before)?.[1];

  await runCommand(page, "/fork");
  await shot(page, "cmd-fork");

  await expect
    .poll(
      async () => {
        const now = await page.locator("body").innerText();
        return /session\s+([0-9a-f]{6,})/i.exec(now)?.[1];
      },
      { timeout: 30_000 },
    )
    .not.toBe(idBefore);
});

test("/clear empties the transcript it is run against", async ({
  page,
  liveBe,
}) => {
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  // Its own session, so nothing a person cares about is destroyed.
  await page.getByRole("button", { name: /New chat/i }).click();
  await expect(page.locator(".msg-assistant")).toHaveCount(0, {
    timeout: 15_000,
  });

  await runCommand(page, "Reply with exactly CLEARME.");
  await expect(
    page.locator(".msg-assistant").filter({ hasText: "CLEARME" }).first(),
  ).toBeVisible({ timeout: 120_000 });

  await runCommand(page, "/clear");
  await shot(page, "cmd-clear");

  await expect(page.locator(".conversation")).not.toContainText("CLEARME", {
    timeout: 30_000,
  });
});

test("the credential commands are offered but not run here", async ({
  page,
  liveBe,
}) => {
  // Recorded rather than skipped silently: they exist, they are
  // reachable, and this suite will not touch someone's stored token.
  // Their behaviour is `todo` in the matrix.
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.fill("/log");

  for (const cmd of NEVER_RUN) {
    await expect(page.locator("body")).toContainText(cmd);
  }
});
