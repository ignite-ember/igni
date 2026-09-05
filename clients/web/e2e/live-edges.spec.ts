/**
 * The edges of a run: cancelling it, leaving it, changing it mid-flight.
 *
 * `live-chat.spec.ts` proves the happy path. These are the transitions
 * either side of it — the ones where the UI can plausibly go on showing
 * a state the backend has left, which is the failure a user describes
 * as "it froze" or "it lost my message".
 *
 * Each test starts a fresh chat and asserts against `.msg-assistant`
 * with a token no earlier run can have produced. Both precautions exist
 * because both were needed: the backend keeps session state, so a
 * reconnect finds the previous answer, and the prompt itself is
 * rendered in the same container as the reply.
 *
 * Requires a real model — see docs/APP_TEST_MATRIX.md for the
 * `IGNI_LIVE_WS` recipe. The bundled fixture's stub model answers
 * "Connection error." to all of this.
 *
 * Two more cases were written and are **not** here: reopening a past
 * session from the sidebar, and rejecting a tool call. Neither could
 * be made to pass or fail for a reason I trusted — the harness kept
 * losing the message across the "+ New chat" re-render — and a test
 * that is red for its own reasons teaches people to ignore the file.
 * They are `todo` in the matrix with what blocked them, which is
 * worth more than an unstable spec.
 */

import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { test } from "./fixtures/live-be";

const OUT = "shots/live";
const READY = /Message (Ember|igni)/;

// A real model over a real network needs more than the project's
// 20s default: the run is killed mid-assertion and the failure reads
// as "element(s) not found", which looks like a broken selector
// rather than a clock. Found by giving a 120s assertion budget inside
// a 20s test and watching the test die first.
test.describe.configure({ mode: "serial", timeout: 180_000 });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

function marker(): string {
  return `TKN${Math.random().toString(36).slice(2, 9).toUpperCase()}`;
}

async function connected(page: Page) {
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    READY,
    { timeout: 60_000 },
  );
}

async function newChat(page: Page) {
  await page.getByRole("button", { name: /New chat/i }).click();
  await expect(page.locator(".msg-assistant")).toHaveCount(0, {
    timeout: 15_000,
  });
}

async function send(page: Page, text: string) {
  const editor = page.locator(".composer-editable");
  await expect(editor).toBeVisible();
  await editor.click();
  await editor.fill(text);
  // Confirm the text actually landed before pressing Enter. Clicking
  // "+ New chat" re-renders the composer, and a fill that raced that
  // re-render sent nothing — the test then waited two minutes for a
  // reply to a message the app never received, and the screenshot
  // showed an empty session.
  await expect(editor).toContainText(text.slice(0, 24), { timeout: 10_000 });
  await editor.press("Enter");
  await expect(page.locator(".msg-user").last()).toContainText(
    text.slice(0, 24),
    { timeout: 15_000 },
  );
}

/**
 * Wait for *some* assistant block to contain `token`.
 *
 * Not `expect(page.locator(".msg-assistant")).toContainText(...)`: a
 * streamed reply renders as several `.msg-assistant` blocks, so the
 * plain locator hits Playwright's strict mode and reports "resolved to
 * 2 elements" — a failure about the test, dressed as a failure about
 * the app. Filtering asks the question that was actually meant: did
 * the assistant say this anywhere?
 */
async function assistantSaid(page: Page, token: string, timeout = 120_000) {
  await expect(
    page.locator(".msg-assistant").filter({ hasText: token }).first(),
  ).toBeVisible({ timeout });
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

async function open(page: Page, ws: string) {
  await page.goto(`/?ws=${encodeURIComponent(ws)}`);
  await connected(page);
}

test("cancelling a run stops it and leaves the composer usable", async ({
  page,
  liveBe,
}) => {
  // The worst outcome here is not "cancel did nothing" — it is a UI
  // stuck in the running state with no way back, which reads as a
  // hang and costs the user the session.
  await open(page, liveBe.wsUrl);
  await newChat(page);

  await send(page, "Count slowly from 1 to 200, one number per line.");

  // Wait until it is genuinely running before cancelling, or the test
  // is cancelling nothing.
  const stop = page.getByRole("button", { name: /stop|cancel/i }).first();
  await expect(stop).toBeVisible({ timeout: 60_000 });
  await shot(page, "10-running");

  await stop.click();
  await shot(page, "11-cancelled");

  // Back to a state that accepts work.
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    READY,
    { timeout: 60_000 },
  );

  // And it actually accepts it.
  const token = marker();
  await send(page, `Reply with exactly ${token}.`);
  await assistantSaid(page, token, 120_000);
});

test("switching session mid-run does not bleed one conversation into another", async ({
  page,
  liveBe,
}) => {
  // Two sessions, one in flight. The answer must land in the session
  // that asked, not in whichever one is on screen when it arrives.
  await open(page, liveBe.wsUrl);
  await newChat(page);

  const slow = marker();
  // Short enough that the queue below drains inside the test budget.
  await send(page, `Count from 1 to 20, then say ${slow}.`);
  await expect(
    page.getByRole("button", { name: /stop|cancel/i }).first(),
  ).toBeVisible({ timeout: 60_000 });

  // Leave for a fresh session while that one runs.
  await newChat(page);
  await shot(page, "12-switched-away");

  // Absence is asserted against a container that always exists.
  // `.msg-assistant` matches nothing in a fresh session, and
  // `expect(locator).not.toContainText()` on an empty locator *fails*
  // rather than passing — it cannot resolve an element to read. The
  // first version reported a cross-session bleed that was not there.
  await expect(page.locator(".conversation")).not.toContainText(slow, {
    timeout: 20_000,
  });

  // Sending here does not start a second run — the app serialises
  // them and *says so*: "Queued — will run after the current turn."
  // My first version expected an immediate reply and spent two
  // minutes proving the app was right. Correct behaviour, clearly
  // communicated, and worth pinning: a queue that ran silently would
  // look like a message that went nowhere.
  const here = marker();
  await send(page, `Reply with exactly ${here}.`);
  await expect(page.locator(".conversation")).toContainText(
    /Queued — will run after the current turn/i,
    { timeout: 30_000 },
  );
  await shot(page, "13-queued");

  // Still no leakage from the session that was running.
  await expect(page.locator(".conversation")).not.toContainText(slow);
});

test("a message queued from another session eventually runs", async ({
  page,
  liveBe,
}) => {
  // **Known failure.** Reproduced three times, 150s each: send in
  // session A, switch to a new session, send there, and the second
  // message sits at "Queued — will run after the current turn"
  // forever. The first run finishes — its session gets its generated
  // title, the composer returns to idle — and the queued one never
  // starts.
  //
  // `test.fixme` rather than deleting it or leaving a permanent red:
  // the expectation is right and the product does not meet it, and
  // that is worth carrying in the report as a declared gap rather
  // than as noise somebody learns to scroll past.
  //
  // Not diagnosed further. The backend log showed transient
  // `API connection error` around the same window, but the proxy was
  // healthy before and after and a CLI call through it answered in
  // 2.0s — so "the upstream was down" does not explain it, and
  // "queued work is dropped" is not yet proven either. Whoever picks
  // this up should start at `get_pending_messages`.
  test.fixme();

  await open(page, liveBe.wsUrl);
  await newChat(page);
  const slow = marker();
  await send(page, `Count from 1 to 20, then say ${slow}.`);
  await expect(
    page.getByRole("button", { name: /stop|cancel/i }).first(),
  ).toBeVisible({ timeout: 60_000 });

  await newChat(page);
  const here = marker();
  await send(page, `Reply with exactly ${here}.`);
  await expect(page.locator(".conversation")).toContainText(/Queued/i, {
    timeout: 30_000,
  });

  await assistantSaid(page, here, 150_000);
});

test("the slash menu opens and lists the built-in commands", async ({
  page,
  liveBe,
}) => {
  // `/` is advertised in the composer's own placeholder — "commands,
  // @ files, $ shell" — so it is a promise the UI makes on every screen.
  await open(page, liveBe.wsUrl);

  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.fill("/");
  await shot(page, "16-slash-menu");

  // At least the ones the backend registers.
  const body = page.locator("body");
  await expect(body).toContainText(/help|model|clear/i, { timeout: 15_000 });
});

