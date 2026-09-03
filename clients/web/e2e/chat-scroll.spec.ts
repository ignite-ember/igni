/**
 * Chat scroll behaviour — pins the contract Virtuoso's
 * ``followOutput="auto"`` is supposed to satisfy in the real App:
 *
 *   • When the user is at the bottom of the list and a new item
 *     is appended, the viewport follows so the new item is visible.
 *   • When the user has scrolled up to read history, appending a
 *     new item must NOT yank them down.
 *
 * Driven by ``?demo=chat-scroll`` (see ``src/dev/ChatScrollDemo.tsx``),
 * which mirrors the App's Virtuoso wiring with no backend coupling.
 * That keeps the test fast + deterministic and pins the exact
 * behaviour without standing up a fixture BE.
 */

import { expect, test, type Page } from "@playwright/test";

async function gotoChatScroll(page: Page) {
  await page.goto("/?demo=chat-scroll");
  await expect(page.getByText("Chat scroll sandbox")).toBeVisible();
}

async function appendMessage(page: Page) {
  await page.getByTestId("append-btn").click();
}

async function scrollToTop(page: Page) {
  await page.getByTestId("scroll-top-btn").click();
}

async function scrollToBottom(page: Page) {
  await page.getByTestId("scroll-bottom-btn").click();
}

async function atBottom(page: Page): Promise<boolean> {
  const text = await page.getByTestId("at-bottom-flag").innerText();
  return /atBottom:\s*true/i.test(text);
}

async function itemCount(page: Page): Promise<number> {
  const text = await page.getByTestId("item-count").innerText();
  const match = text.match(/items:\s*(\d+)/);
  return match ? Number(match[1]) : NaN;
}

async function waitForAtBottom(page: Page, want: boolean) {
  await expect
    .poll(async () => atBottom(page), { timeout: 4_000 })
    .toBe(want);
}

/**
 * Virtuoso's scroll offset, once it has stopped moving.
 *
 * F129. The "does not yank the user down" test used to assert which
 * rows were mounted — ``row-1`` visible, ``row-61`` not. Under a full
 * eight-worker run that failed both ways: sometimes ``row-1`` was not
 * in the DOM yet, sometimes ``row-61`` was. Neither is the contract.
 * A virtualiser is free to mount and unmount whatever it likes while a
 * scroll settles; what the user cares about is that their position did
 * not move, and that is a number.
 *
 * Settling is two consecutive equal readings, because a smooth scroll
 * in flight gives a different answer each frame and a single reading
 * cannot tell "arrived" from "passing through".
 */
async function settledScrollTop(page: Page): Promise<number> {
  const read = () =>
    page.evaluate(() => {
      // Virtuoso marks its own scroller. Named exactly rather than
      // "the first descendant that overflows", which is what this
      // helper tried first: that heuristic found the right node here
      // by luck, and a wrong node reads a constant 0 — which is a
      // test that passes without measuring anything.
      const el = document.querySelector<HTMLElement>(
        "[data-virtuoso-scroller]",
      );
      if (!el) throw new Error("no [data-virtuoso-scroller] on the page");
      if (el.scrollHeight <= el.clientHeight) {
        throw new Error(
          `the scroller does not overflow (${el.scrollHeight} <= ` +
            `${el.clientHeight}); there is no scroll position to read`,
        );
      }
      return el.scrollTop;
    });

  let previous = await read();
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(50);
    const current = await read();
    if (current === previous) return current;
    previous = current;
  }
  return previous;
}

test.describe("chat scroll — Virtuoso followOutput contract", () => {
  test("seeds 60 rows and starts NOT at bottom (long list collapsed to top)", async ({
    page,
  }) => {
    await gotoChatScroll(page);
    // Sanity: seed count matches what the demo declares.
    await expect.poll(async () => itemCount(page)).toBe(60);
    // Virtuoso's default mounts at the top — the user has to
    // scroll down before "at bottom" goes true.
    await expect(atBottom(page)).resolves.toBe(false);
  });

  test("scrolling to the LAST row flips atBottom to true", async ({ page }) => {
    await gotoChatScroll(page);
    // Virtuoso virtualizes — row-60 isn't in the DOM until we
    // scroll to it. Use Virtuoso's own scrollToIndex via the
    // demo button so we don't have to find / scroll-into-view
    // an element that doesn't exist yet.
    await scrollToBottom(page);
    await waitForAtBottom(page, true);
  });

  test("appending while at the bottom keeps the new row visible (followOutput=auto)", async ({
    page,
  }) => {
    // The contract: I'm reading at the tail; the agent (or me)
    // adds a message; my view must follow so I see the new bubble
    // without reaching for a scroll-to-bottom button.
    await gotoChatScroll(page);
    await scrollToBottom(page);
    await waitForAtBottom(page, true);

    await appendMessage(page);
    // The new row exists and is in the viewport — the actual
    // assertion the user cares about.
    const newRow = page.getByTestId("row-61");
    await expect(newRow).toBeVisible();
    // And atBottom stays true after the follow.
    await waitForAtBottom(page, true);
  });

  test("appending while scrolled away does NOT yank the user down", async ({
    page,
  }) => {
    // Inverse contract: I'm reading history; an append must not
    // disrupt me. Critical for trust — agents that interrupt your
    // reading position get muted.
    //
    // Virtuoso initial-mount quirk: mounting an empty-then-populated
    // list often leaves the internal "atBottom" state in an
    // ambiguous "haven't received a scroll event yet" position.
    // First-scroll-up needs to be a real scroll (not just
    // scrollToIndex(0)) so Virtuoso emits the proper at-bottom-
    // -false transition that ``followOutput="auto"`` reads on the
    // next data change. Scroll the inner scroller directly to
    // ~halfway through the list to be unambiguous.
    await gotoChatScroll(page);
    await scrollToBottom(page);
    await waitForAtBottom(page, true);
    // Now scroll WAY up so atBottom flips false (genuine user
    // intent to read history).
    await scrollToTop(page);
    await waitForAtBottom(page, false);
    // Where the reader is, once the scroll has stopped moving. This is
    // the thing the contract is about; which rows happen to be mounted
    // around it is the virtualiser's business.
    const before = await settledScrollTop(page);

    await appendMessage(page);
    await waitForAtBottom(page, false);
    const after = await settledScrollTop(page);
    expect(
      after,
      `appending moved the reader from ${before}px to ${after}px`,
    ).toBe(before);
  });
});
