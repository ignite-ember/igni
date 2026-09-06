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

import { test, whyNoModel } from "./fixtures/live-be";

const OUT = "shots/slash";

// `serial`, like the sibling live specs. Under `IGNI_LIVE_WS` every
// worker shares one backend, and one backend has one current session:
// run these four-up and `/clear` opens onto the transcript `/fork` is
// still writing, then fails looking for its own token. That failure is
// about the harness, and it took a screenshot showing another test's
// conversation to see it.
test.describe.configure({ mode: "serial", timeout: 120_000 });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

/**
 * Commands that only read state. Safe to run anywhere.
 *
 * Each names **the surface the command is supposed to open**, and the
 * assertion runs inside it. The first version of this list asserted
 * `page.locator("body")` against patterns like `/session/i` and
 * `/model|DeepSeek/i` — and the footer names the session, the model
 * picker names the model, and the composer's own hint line reads "/
 * commands". Every one of those matched on a page where the command
 * had done nothing at all. Seven tests that could not fail, in a file
 * whose whole purpose was to prove the commands work.
 *
 * `where` is taken from `App.tsx`'s `runCommand` switch, not guessed:
 * `help`/`agents`/`skills`/`mcp` call `setPanel`, which renders inside
 * `Drawer`'s `aside.drawer`; `sessions` calls `setSidebarOpen(true)`;
 * `model` opens the composer's `.model-menu`; `/ctx` has no case at
 * all and falls through to the transcript.
 *
 * `before` exists for one of them. Deleting the `Enter` keypress from
 * `runCommand` — a command never submitted — turned six of these red
 * and left `/sessions` green: on a desktop viewport `sidebarOpen`
 * starts `true`, so the surface it opens was already open. A test that
 * passes when the command does not run is the thing this whole file
 * was rewritten to stop doing, so `/sessions` closes the sidebar
 * first.
 */
const READ_ONLY: {
  cmd: string;
  where: string;
  shows: RegExp;
  before?: (page: Page) => Promise<void>;
}[] = [
  { cmd: "/help", where: ".drawer", shows: /\/compact/ },
  // `/context|token|floor/i` is what this used to say, and `/ctx` was
  // raising `PydanticUserError: ContextBreakdownView is not fully
  // defined` on every single invocation — a message that contains
  // "Context", rendered into the same `.conversation` this asserts on.
  // The test was green the whole time the command was broken.
  //
  // `% of total` comes from the card's own template and appears in no
  // error path.
  { cmd: "/ctx", where: ".conversation", shows: /% of total/ },
  {
    cmd: "/sessions",
    where: ".sidebar:not(.closed)",
    shows: /session/i,
    before: async (page) => {
      await page.getByTitle("Toggle sessions").click();
      await expect(page.locator(".sidebar.closed")).toBeVisible();
    },
  },
  { cmd: "/model", where: ".model-menu", shows: /\w/ },
  { cmd: "/agents", where: ".drawer", shows: /agent/i },
  { cmd: "/skills", where: ".drawer", shows: /skill/i },
  { cmd: "/mcp", where: ".drawer", shows: /mcp|server/i },
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
  // The menu itself, not the page. The composer's own hint line reads
  // "/ commands, @ files, $ shell" and the welcome panel names nine
  // commands outright, so a body-scoped assertion here passes with no
  // menu open at all.
  const menu = page.locator(".popup-menu");
  await expect(menu).toContainText(/\/help/, { timeout: 15_000 });
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
  await expect(menu).toContainText(/\/model/, { timeout: 10_000 });
  // Scoped, so this cannot be satisfied by the menu having closed:
  // `.not.toContainText` fails on a locator that matches nothing.
  await expect(menu).not.toContainText(/\/logout/);
  await shot(page, "01-menu-filtered");
});

for (const { cmd, where, shows, before } of READ_ONLY) {
  test(`${cmd} opens its surface`, async ({ page, liveBe }) => {
    // The bar is deliberately low and the point is coverage: a command
    // in the menu that errors, or renders an empty panel, is worse
    // than one that is not offered. What each panel *should* contain
    // is a separate judgement per command.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);
    if (before) await before(page);

    await runCommand(page, cmd);
    await shot(page, `cmd${cmd.replace("/", "-")}`);

    // The surface exists at all. Asserted separately from its content
    // so a command that opens nothing fails saying *that*, rather than
    // reporting a text mismatch inside an element that is not there.
    const surface = page.locator(where).first();
    await expect(surface, `${cmd} did not open ${where}`).toBeVisible({
      timeout: 30_000,
    });
    await expect(surface).toContainText(shows, { timeout: 30_000 });
    // Nothing blew up on the way. Scoped to the surface deliberately:
    // `.not.toContainText` on a locator that matches nothing *fails*,
    // so this cannot be satisfied by a panel that never opened.
    // `errors.pydantic.dev` and `not fully defined` are here because
    // a Pydantic forward-ref failure is what `/ctx` was shipping, and
    // it reads as prose rather than as a crash — no traceback, no
    // "unhandled", just a paragraph with a docs link in a chat bubble.
    await expect(surface).not.toContainText(
      /traceback|unhandled|is not defined|not fully defined|errors\.pydantic\.dev/i,
    );
  });
}

test(
  "/fork continues the conversation under a new id",
  { tag: "@needs-model" },
  async ({ page, liveBe }) => {
    // Needs a reply before it can fork a session that has said
    // something — declared skip rather than a red test about the app.
    const why = whyNoModel();
    if (why) test.skip(true, why);

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
  },
);

test(
  "/clear empties the transcript it is run against",
  { tag: "@needs-model" },
  async ({ page, liveBe }) => {
    // Same: the transcript has to contain something before emptying it
    // proves anything.
    const why = whyNoModel();
    if (why) test.skip(true, why);

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
  },
);

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
  // `pressSequentially`, not `fill` — see the filter test above: the
  // composer eats the leading slash as a mode switch, and a
  // programmatic value never opens the menu.
  await editor.pressSequentially("/log");

  const menu = page.locator(".popup-menu");
  for (const cmd of NEVER_RUN) {
    await expect(menu).toContainText(cmd, { timeout: 15_000 });
  }
});
