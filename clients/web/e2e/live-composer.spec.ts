/**
 * The composer's other two input modes: `@` files and `$` shell.
 *
 * The composer advertises three on every screen — "/ commands, @
 * files, $ shell" — and only `/` had a test. `@` is how a user points
 * the agent at a file, and `$` is how they run a command themselves;
 * both are primary, both were unexercised.
 *
 * The gap was not academic. Driving `@` by hand found that it stops
 * working after "+ New chat": the session id rotates one RPC round
 * trip after the click, the Composer re-hydrates its draft from
 * `draft:<sessionId>`, and the `@` the mention parser needs is taken
 * out of the editor. The picker stays on screen, filters to nothing,
 * and the query goes to the model as chat. Fixed in `App.tsx`
 * (`carryDraft`); pinned below.
 *
 * `$` and the model's `run_shell_command` tool are **different code
 * paths** and this file only covers the first. `$` is the user's own
 * command: it runs through the `run_shell` RPC with no approval
 * dialog, because the person who typed it has already consented. The
 * tool path — model decides, user approves — is `live-chat.spec.ts`.
 *
 * Needs a backend, not a model: nothing here waits on a reply.
 */

import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test } from "./fixtures/live-be";

const OUT = "shots/composer";

// Serial: under `IGNI_LIVE_WS` every worker shares one backend, and
// one backend has one current session. See live-slash.spec.ts.
test.describe.configure({ mode: "serial", timeout: 120_000 });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

async function connected(page: Page) {
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 60_000 },
  );
}

/** The session id as the footer prints it. */
async function sessionId(page: Page): Promise<string | undefined> {
  const body = await page.locator("body").innerText();
  return /session\s+([0-9a-f]{6,})/i.exec(body)?.[1];
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

test.describe("@ file mentions", () => {
  test("the picker opens on @ and narrows as you type", async ({
    page,
    liveBe,
  }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    // `pressSequentially`, not `fill`: the mention parser reads the
    // token under the caret, and a programmatic value change does not
    // put a caret anywhere.
    await editor.pressSequentially("@");

    const items = page.locator(".popup-item");
    await expect(items.first()).toBeVisible({ timeout: 15_000 });
    const openedWith = await items.count();
    await shot(page, "01-mention-open");

    await editor.pressSequentially("calc");
    // Narrowing means *fewer* and *right*, and both halves matter: a
    // picker that always returns the whole tree passes any assertion
    // about the file being present.
    await expect(items.first()).toContainText("calc.py", { timeout: 15_000 });
    expect(await items.count()).toBeLessThan(openedWith);
    await shot(page, "02-mention-filtered");
  });

  test("accepting an entry inserts a pill, not the raw path", async ({
    page,
    liveBe,
  }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("@calc");
    await expect(page.locator(".popup-item").first()).toContainText("calc.py", {
      timeout: 15_000,
    });
    await editor.press("Enter");

    // The pill carries the resolved path in an attribute. Asserting
    // the *text* would pass on a plain-text "calc.py" the mention
    // pipeline never resolved — which is exactly the broken state
    // this file was written after finding.
    const pill = editor.locator(".file-pill");
    await expect(pill).toHaveAttribute("data-pill-path", "calc.py");
    // Accepting closes the picker; leaving it open swallows the next
    // Enter, which is how a user ends up sending nothing.
    await expect(page.locator(".popup-menu")).toHaveCount(0);
    await shot(page, "03-mention-accepted");
  });

  test("a mention is sent as a resolved reference", async ({
    page,
    liveBe,
  }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("@calc");
    await expect(page.locator(".popup-item").first()).toContainText("calc.py", {
      timeout: 15_000,
    });
    await editor.press("Enter");
    await editor.pressSequentially(" summarise this");
    await editor.press("Enter");

    // The user's turn carries a "Referenced:" line naming the file.
    // That is the whole point of `@`: without it the model gets a
    // sentence about a file it cannot see. Rendered on submit, so no
    // model reply is waited on here.
    await expect(page.locator(".conversation")).toContainText(
      /Referenced:\s*calc\.py/,
      { timeout: 30_000 },
    );
    await shot(page, "04-mention-sent");
  });

  test("the picker still works right after + New chat", async ({
    page,
    liveBe,
  }) => {
    // The regression, stated as the user's sequence: start a new chat,
    // reach for a file. `sessionId` rotates asynchronously after the
    // click, and the Composer's draft-hydration effect used to blank
    // the editor — taking the `@` with it. The picker then listed
    // every file and filtered to none, so the app looked like it had
    // lost @-mentions entirely.
    //
    // Deliberately does NOT wait for the rotation to land. Waiting is
    // what made this pass while broken: the same steps with a settle
    // in the middle work fine, and a test that settles first is a test
    // of the state after the race rather than of the race.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);
    const before = await sessionId(page);

    await page.getByRole("button", { name: /New chat/i }).click();

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("@");
    await expect(page.locator(".popup-item").first()).toBeVisible({
      timeout: 15_000,
    });
    await editor.pressSequentially("calc");

    await expect(page.locator(".popup-item").first()).toContainText("calc.py", {
      timeout: 15_000,
    });
    // The `@` is still in the editor for the parser to find. This is
    // the actual thing that broke; the picker's contents were the
    // symptom.
    await expect(editor).toContainText("calc");
    await shot(page, "05-mention-after-new-chat");

    // And the id really did rotate while we typed — otherwise the
    // race never happened and the assertions above proved nothing.
    await expect
      .poll(() => sessionId(page), { timeout: 30_000 })
      .not.toBe(before);
  });
});

test.describe("$ shell", () => {
  test("$ enters shell mode and Backspace leaves it", async ({
    page,
    liveBe,
  }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("$");
    // The `$` is consumed as a mode switch, exactly like `/`. The
    // placeholder is the only thing that tells the user what Enter
    // will now do, so it is the assertion.
    await expect(editor).toHaveClass(/mode-shell/);
    await expect(editor).toHaveAttribute("data-placeholder", /Shell command/i);
    await shot(page, "06-shell-mode");

    // The placeholder promises "Backspace to return to chat". Honour
    // it, or the user is stuck in a mode they cannot see how to exit.
    await editor.press("Backspace");
    await expect(editor).not.toHaveClass(/mode-shell/);
    await expect(editor).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );
  });

  test("a $ command runs and shows its output, with no approval", async ({
    page,
    liveBe,
  }) => {
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("$echo IGNI_SHELL_OK");
    await editor.press("Enter");

    const out = page.locator(".shell-output").last();
    await expect(out).toContainText("IGNI_SHELL_OK", { timeout: 30_000 });
    // The command is echoed above its output, so the transcript shows
    // what produced what.
    await expect(out.locator(".prompt-line")).toContainText("echo");
    // No approval dialog. `$` is the user's own command — asking them
    // to authorise what they just typed would be theatre. Scoped to
    // the dialog's own class so this fails if one appears rather than
    // passing on an empty page.
    await expect(page.locator(".hitl-card")).toHaveCount(0);
    await shot(page, "07-shell-ran");
  });

  test("a failing $ command reports its exit code", async ({
    page,
    liveBe,
  }) => {
    // Silence on failure is the trap: the command did not run, the
    // output pane is empty, and nothing distinguishes that from a
    // command that printed nothing and succeeded.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.pressSequentially("$exit 7");
    await editor.press("Enter");

    await expect(page.locator(".shell-output").last()).toContainText(/exit 7/, {
      timeout: 30_000,
    });
    await shot(page, "08-shell-failed");
  });
});
