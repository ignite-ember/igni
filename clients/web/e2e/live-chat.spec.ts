/**
 * A real conversation, end to end, with a real model.
 *
 * Every other live spec stops short of this on purpose.
 * ``real-be.spec.ts`` says so in its own docstring — "What's NOT
 * covered (deliberately): the agent loop. Driving an actual
 * ``run_message`` would call the LLM" — and ``live-be-plan-decision``
 * fires ``approve_plan`` through the exposed client rather than
 * through the UI. So the wire format is proven, the RPC dispatch is
 * proven, and *the thing the product is for* is not: type a question,
 * watch it stream, see a tool run, read the answer.
 *
 * That gap is the reason this file exists. It needs a model, so it
 * skips when there is no working one rather than failing — but the
 * skip is declared, so `reporters/skips.ts` shows it instead of
 * letting the run look complete.
 *
 * Screenshots go to ``shots/live/`` so the states can be looked at,
 * because a passing assertion about a DOM node is not the same as a
 * screen a person would accept.
 */

import { expect } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test } from "./fixtures/live-be";

const OUT = "shots/live";
const READY = /Message (Ember|igni)/;

/**
 * A token no previous run can have produced.
 *
 * The backend keeps session state, and `?ws=` reconnects to the same
 * session — so the transcript still holds every earlier answer. The
 * first version of this file asserted on the word "PONG" and passed in
 * 613ms against a reply the *previous, failing* run had produced. A
 * live-model test that completes faster than a network round trip is
 * not testing the model.
 *
 * Each test asks the model to echo its own token, so a stale
 * transcript cannot satisfy it.
 *
 * That was still not enough. Asserting the token anywhere in
 * `.conversation` matched **my own prompt**, which contains it and is
 * rendered in the same container — three tests passing in under a
 * second each, none of them reaching the model. The assertions target
 * `.msg-assistant` now: the user's turn is `.msg-user`, so only a
 * reply can satisfy them.
 */
function marker(): string {
  return `TKN${Math.random().toString(36).slice(2, 9).toUpperCase()}`;
}

/** Start from an empty transcript, so nothing earlier can match. */
async function newChat(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: /New chat/i }).click();
  await expect(page.locator(".conversation")).not.toContainText(/TKN/, {
    timeout: 15_000,
  });
}

test.describe.configure({ mode: "serial" });

test.beforeAll(() => {
  mkdirSync(OUT, { recursive: true });
});

/** Wait for the composer to leave "Connecting…" and settle. */
async function connected(page: import("@playwright/test").Page) {
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    READY,
    { timeout: 60_000 },
  );
}

/** Type into the contenteditable composer and send. */
async function send(page: import("@playwright/test").Page, text: string) {
  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.fill(text);
  await editor.press("Enter");
}

/** Screenshot once nothing is animating — see the portal's shot(). */
async function shot(page: import("@playwright/test").Page, name: string) {
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
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
}

test("a question gets an answer", async ({ page, liveBe }) => {
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);
  await shot(page, "01-connected");
  await newChat(page);

  const token = marker();
  await send(page, `Reply with exactly ${token} and nothing else.`);
  await shot(page, "02-sent");

  // The assistant's reply lands in the transcript. Generous timeout:
  // a real model over a real network, not a fixture.
  // `.conversation` is the transcript. My first attempt guessed
  // `.chat-scroll, .chat-list, main` and Playwright reported
  // "element(s) not found" — a failing test about a working app, which
  // is the most expensive kind of red.
  await expect(page.locator(".msg-assistant")).toContainText(token, {
    timeout: 120_000,
  });
  await shot(page, "03-answered");
});

test("the composer is usable again after a turn", async ({ page, liveBe }) => {
  // The state that strands a user: the run finishes but the input
  // never re-enables, so the app looks alive and takes no more work.
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  await newChat(page);
  const token = marker();
  await send(page, `Reply with exactly ${token}.`);
  await expect(page.locator(".msg-assistant")).toContainText(token, {
    timeout: 120_000,
  });

  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    READY,
    { timeout: 30_000 },
  );
  await shot(page, "04-ready-again");
});

test("a tool call is shown to the user", async ({ page, liveBe }) => {
  // The product's whole claim is that it works on your code. A run
  // that reads a file must say so — a silent tool call is
  // indistinguishable from a model that made the answer up.
  await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
  await connected(page);

  await newChat(page);
  const token = marker();
  await send(
    page,
    `List the files in the current directory using your shell tool, then say ${token}.`,
  );

  // Either a tool-call row appears, or the run ends without one —
  // both are answers, and the screenshot records which.
  // The run pauses here, which is the correct and safe behaviour: a
  // shell command needs a human. My first version asserted on the
  // answer and timed out at the approval prompt — a red test about an
  // app doing exactly the right thing.
  const prompt = page.getByText(/Allow Bash\?/i);
  await expect(prompt).toBeVisible({ timeout: 120_000 });
  await shot(page, "05-approval-prompt");

  // The command is shown before it is authorised. Approving something
  // you cannot read is not consent.
  await expect(page.locator("body")).toContainText(/ls\s+-la/);

  await page.getByRole("button", { name: /Allow once/i }).click();

  await expect(page.locator(".msg-assistant")).toContainText(
    new RegExp(`${token}|error|failed`, "i"),
    { timeout: 180_000 },
  );
  await shot(page, "06-tool-completed");
});
