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
 * That gap is the reason this file exists.
 *
 * It needs a model that answers, so it skips when there is not one —
 * `whyNoModel`, in the `beforeEach` below.
 *
 * That skip is new, and the comment here claimed it for a while before
 * it existed. There was none: `fixtures/live-be.ts` skips when there
 * is no *backend*, and given one it writes `e2e-wire-format-stub`,
 * pointing at `http://127.0.0.1:9`, where nothing listens. So on a
 * stub-spawned run — which is what CI does, `npx playwright test` with
 * no `IGNI_LIVE_WS` — these tests did not skip, they went red, blaming
 * the app for a missing assistant bubble that no model was ever asked
 * for. A false claim in a comment is worse than no claim: it describes
 * a safeguard nobody then builds.
 *
 * Run it against a real model:
 *
 *     IGNI_LIVE_WS=ws://127.0.0.1:PORT npx playwright test live-chat
 *
 * These now skip in CI rather than failing there, which is honest but
 * not the same as covered. docs/APP_TEST_MATRIX.md says so.
 *
 * Screenshots go to ``shots/live/`` so the states can be looked at,
 * because a passing assertion about a DOM node is not the same as a
 * screen a person would accept.
 */

import { expect } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test, whyNoModel } from "./fixtures/live-be";

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
  // Emptiness measured by the absence of assistant blocks, not by the
  // absence of a token string: session *titles* in the sidebar carry
  // earlier tokens, and a text-absence assertion over a broad
  // container picks them up.
  await expect(page.locator(".msg-assistant")).toHaveCount(0, {
    timeout: 15_000,
  });
}

// A real model over a real network needs more than the project's
// 20s default: the run is killed mid-assertion and the failure reads
// as "element(s) not found", which looks like a broken selector
// rather than a clock. Found by giving a 120s assertion budget inside
// a 20s test and watching the test die first.
test.describe.configure({ mode: "serial", timeout: 180_000 });

test.beforeAll(() => {
  mkdirSync(OUT, { recursive: true });
});

// Every test here waits on a model. Declared skip, not silence — see
// `whyNoModel` and `reporters/skips.ts`.
test.beforeEach(() => {
  const why = whyNoModel();
  if (why) test.skip(true, why);
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

/**
 * Wait for *some* assistant block to contain `token`.
 *
 * A streamed reply renders as several `.msg-assistant` blocks, so a
 * plain `toContainText` on that locator hits Playwright's strict mode
 * and reports "resolved to 2 elements" — a failure about the test,
 * dressed as a failure about the app.
 */
async function assistantSaid(
  page: import("@playwright/test").Page,
  token: string,
  timeout = 120_000,
) {
  await expect(
    page.locator(".msg-assistant").filter({ hasText: token }).first(),
  ).toBeVisible({ timeout });
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

test(
  "a question gets an answer",
  { tag: "@needs-model" },
  async ({ page, liveBe }) => {
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
    await assistantSaid(page, token, 120_000);
    await shot(page, "03-answered");
  },
);

test(
  "the composer is usable again after a turn",
  { tag: "@needs-model" },
  async ({ page, liveBe }) => {
    // The state that strands a user: the run finishes but the input
    // never re-enables, so the app looks alive and takes no more work.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);
    await connected(page);

    await newChat(page);
    const token = marker();
    await send(page, `Reply with exactly ${token}.`);
    await assistantSaid(page, token, 120_000);

    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      READY,
      { timeout: 30_000 },
    );
    await shot(page, "04-ready-again");
  },
);

test(
  "a tool call is shown to the user",
  { tag: "@needs-model" },
  async ({ page, liveBe }) => {
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
    // *A* command is shown, not a specific one. Pinning `ls -la` made
    // the test depend on which command the model happened to pick — it
    // chose differently on a later run and the test failed about the
    // app. What matters is that whatever is about to run is legible
    // before it is authorised: approving something you cannot read is
    // not consent.
    // Read inside the approval dialog, not the page. Two things were
    // wrong with the wider version. `.conversation, body` falls back to
    // `body` whenever the transcript selector misses, which is the whole
    // page; and `COMMAND` as an alternative matched the dialog's own
    // *label* — so a dialog that printed the word "Command" and dropped
    // the command would have passed the test whose entire subject is
    // that you can read what you are authorising.
    const dialog = page.locator(".hitl-card").first();
    await expect(dialog).toBeVisible();
    // `.hitl-arg-val` is the value column of `HitlArgsView`'s grid —
    // labels live in `.hitl-arg-key` and cannot leak in. So this reads
    // the command itself, which is the claim being made.
    //
    // A command line, not a specific one: pinning `ls -la` made the test
    // depend on which command the model happened to pick, and it chose
    // differently on a later run. Any shell verb will do — what must not
    // be possible is passing with none of them on screen.
    await expect(dialog.locator(".hitl-arg-val").first()).toContainText(
      /\b(ls|find|echo|cat|pwd)\b/i,
    );

    await page.getByRole("button", { name: /Allow once/i }).click();

    // The token, and only the token. `|error|failed` used to be an
    // alternative here, which meant the assertion was satisfied by the
    // run failing — a test named "a tool call is shown to the user" that
    // passed when the tool call did not work. If the run errors, this
    // should go red and the screenshot below says why.
    await expect(
      page.locator(".msg-assistant").filter({ hasText: token }).first(),
    ).toBeVisible({ timeout: 180_000 });
    await shot(page, "06-tool-completed");
  },
);
