/**
 * The two places you reach a feature without typing: the header tools
 * menu and the welcome cards.
 *
 * Thirteen entries and nine cards, none of them clicked by any test.
 * They matter separately from `live-slash.spec.ts` even though they
 * run the same commands, for three reasons:
 *
 * 1. They are the discovery path. `TOOLS_MENU` is documented in-code
 *    as "each opens its feature's UI directly (no slash command
 *    visible to the user)" — for anyone who has not learned the
 *    commands, this *is* the product's surface.
 * 2. They take the `echo=false` branch of `runCommand`, which the
 *    typed path never takes. Its whole job is to not fake a typed
 *    message into the transcript, and nothing checked that.
 * 3. A menu entry naming a command that does not exist, or a card
 *    wired to nothing, fails silently — the user clicks and the app
 *    sits there. That is exactly what `/plugin` did.
 *
 * The lists are read out of the DOM and their sizes asserted, so a
 * card added without a row here fails rather than going unnoticed.
 *
 * Needs a backend, not a model.
 */

import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test } from "./fixtures/live-be";

const OUT = "shots/affordances";

// Serial, like the sibling live specs: every worker shares one
// `IGNI_LIVE_WS` backend and every test here starts a new chat, which
// rotates that backend's current session out from under whatever else
// is mid-click.
//
// The cost is that Playwright's `serial` skips the rest of the file
// after a failure, so one broken entry hides the other twenty-three.
// When triaging, `-g "opens its feature"` or `-g "card runs it"` gives
// the whole picture; a correct partial answer beats a complete wrong
// one.
test.describe.configure({ mode: "serial", timeout: 120_000 });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

/** Where each command puts its answer. Taken from `App.tsx`'s
 *  `runCommand` switch, the same source `live-slash.spec.ts` uses. */
type Lands = "drawer" | "transcript" | "either";

const LANDS: Record<string, Lands> = {
  "/mcp": "drawer",
  "/codeindex": "drawer",
  "/agents": "drawer",
  "/skills": "drawer",
  "/plugins": "drawer",
  "/hooks": "drawer",
  "/loop": "drawer",
  "/schedule": "drawer",
  "/watcher": "drawer",
  "/help": "drawer",
  "/compact": "transcript",
  "/ctx": "transcript",
  // A client-side intercept, not a backend command — it appends an
  // info line listing what it found.
  "/workflows": "transcript",
  // Opens the panel when the index is up, explains itself when it is
  // not. Both are correct answers; silence is not.
  "/knowledge": "either",
};

/** The header tools menu, in `App.tsx`'s `TOOLS_MENU` order. */
const TOOLS = [
  "MCP servers",
  "CodeIndex",
  "Agents",
  "Skills",
  "Plugins",
  "Knowledge",
  "Hooks",
  "Loop",
  "Scheduled tasks",
  "Watcher",
  "Compact context",
  "Context breakdown",
  "Help",
];

const TOOL_COMMAND: Record<string, string> = {
  "MCP servers": "/mcp",
  CodeIndex: "/codeindex",
  Agents: "/agents",
  Skills: "/skills",
  Plugins: "/plugins",
  Knowledge: "/knowledge",
  Hooks: "/hooks",
  Loop: "/loop",
  "Scheduled tasks": "/schedule",
  Watcher: "/watcher",
  "Compact context": "/compact",
  "Context breakdown": "/ctx",
  Help: "/help",
};

/** The welcome hero's cards, in render order. */
const CARDS = [
  "/agents",
  "/skills",
  "/workflows",
  "/codeindex",
  "/schedule",
  "/loop",
  "/mcp",
  "/plugins",
  "/knowledge",
];

async function connected(page: Page) {
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 60_000 },
  );
}

/** An empty transcript, so the welcome hero renders and `.msg-user`
 *  counts mean something. */
async function emptyChat(page: Page) {
  await page.getByRole("button", { name: /New chat/i }).click();
  await expect(page.locator(".msg-assistant")).toHaveCount(0, {
    timeout: 15_000,
  });
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

/** Assert the command's answer arrived where `App.tsx` sends it. */
async function landed(page: Page, command: string) {
  const where = LANDS[command];
  const drawer = page.locator(".drawer").first();
  const conversation = page.locator(".conversation");

  if (where === "drawer") {
    await expect(drawer, `${command} opened no drawer`).toBeVisible({
      timeout: 30_000,
    });
    await expect(drawer).not.toContainText(
      /session routing failed|traceback|has no attribute/i,
    );
    return;
  }
  if (where === "transcript") {
    // Non-empty, and specifically not a crash. `.not.toContainText` on
    // `.conversation` alone would pass on a page with no transcript,
    // so the emptiness check comes first.
    await expect(conversation).not.toBeEmpty({ timeout: 30_000 });
    await expect(conversation).not.toContainText(
      /session routing failed|traceback|has no attribute/i,
    );
    return;
  }
  await expect
    .poll(
      async () =>
        (await drawer.isVisible().catch(() => false)) ||
        (await conversation.innerText().catch(() => "")).trim().length > 0,
      { timeout: 30_000 },
    )
    .toBe(true);
}

test.describe("header tools menu", () => {
  test("it offers exactly the entries App.tsx defines", async ({
    page,
    liveBe,
  }) => {
    // Read, not assumed. A fourteenth entry added without a row in
    // TOOLS_COMMAND would otherwise go unclicked and unnoticed —
    // which is how `/watcher` came to be reachable from here and from
    // nowhere else in the UI.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    await page.getByTitle("Tools & commands").click();
    const items = page.locator(".popup-menu .popup-item .cmd");
    await expect(items).toHaveCount(TOOLS.length, { timeout: 15_000 });
    expect(await items.allInnerTexts()).toEqual(TOOLS);
    await shot(page, "00-tools-menu");
  });

  for (const label of TOOLS) {
    test(`"${label}" opens its feature`, async ({ page, liveBe }) => {
      await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
      await connected(page);
      await emptyChat(page);

      await page.getByTitle("Tools & commands").click();
      await page
        .locator(".popup-menu .popup-item")
        .filter({ hasText: label })
        .first()
        .click();
      await shot(page, `tool-${label.replace(/\W+/g, "-").toLowerCase()}`);

      await landed(page, TOOL_COMMAND[label]);
      // The menu closes on click. One that stays open covers the
      // panel it just opened.
      await expect(page.locator(".popup-menu")).toHaveCount(0);
      // `echo=false`: the point of the menu is that it does not put a
      // command the user never typed into their transcript.
      await expect(page.locator(".msg-user")).toHaveCount(0);
    });
  }
});

test.describe("welcome cards", () => {
  test("the hero offers exactly the nine cards", async ({ page, liveBe }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);
    await emptyChat(page);

    const caps = page.locator(".welcome-cap code");
    await expect(caps).toHaveCount(CARDS.length, { timeout: 15_000 });
    expect(await caps.allInnerTexts()).toEqual(CARDS);
    await shot(page, "01-welcome");
  });

  for (const cmd of CARDS) {
    test(`the ${cmd} card runs it`, async ({ page, liveBe }) => {
      await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
      await connected(page);
      await emptyChat(page);

      await page.locator(".welcome-cap").filter({ hasText: cmd }).click();
      await shot(page, `card-${cmd.replace("/", "")}`);

      await landed(page, cmd);
      await expect(page.locator(".msg-user")).toHaveCount(0);
    });
  }
});
