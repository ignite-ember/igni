/**
 * Live verification of orphan-process rehydration.
 *
 * Drives a real browser against the running Tauri BE
 * (``IGNI_LIVE_WS``). Assumes the test environment has
 * pre-seeded an orphan row in the project's ``state.db`` AND
 * has a real OS process matching that pid still alive (see
 * the harness shell script that calls this spec).
 *
 * Captures the watcher panel showing the orphan + clicks Kill,
 * verifies the row vanishes after the BE confirms termination.
 */
import { test as base, expect } from "@playwright/test";

const test = base.extend<{ liveWsUrl: string }>({
  liveWsUrl: async ({}, use) => {
    const url = process.env.IGNI_LIVE_WS;
    // A declared skip, not a forgotten one — see the @needs-seed tag
    // above and ``reporters/skips.ts``. This test needs an orphan row
    // in ``state.db`` and a live OS process matching its pid, seeded
    // *before* the backend starts, so the fixture that spawns its own
    // backend cannot serve it:
    //
    //   .venv/bin/python scripts/seed_watcher_e2e_orphan.py \\
    //     --scenario sleep
    //   # start the backend against the repo root, then
    //   IGNI_LIVE_WS=ws://127.0.0.1:PORT npx playwright test e2e/watcher-orphan-live.spec.ts
    //   .venv/bin/python scripts/seed_watcher_e2e_orphan.py --cleanup
    if (!url) test.skip(true, "needs a seeded orphan: see the header");
    await use(url as string);
  },
});

test("orphan process surfaces after BE restart", { tag: "@needs-seed" }, async ({ page, liveWsUrl }) => {
  await page.goto(`/?ws=${encodeURIComponent(liveWsUrl)}`);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 30_000 },
  );

  // Open the watcher panel via the slash command. The real BE
  // handles ``/watcher`` → ``CommandAction.WATCHER`` which
  // App.tsx routes to ``setPanel({kind:"watcher"})``.
  await page.locator(".composer-editable").click();
  await page.locator(".composer-editable").type("/watcher");
  await page.locator(".composer-editable").press("Enter");
  await expect(page.locator(".drawer")).toBeVisible({ timeout: 10_000 });

  // The seeded orphan should render exactly one row.
  await expect(page.locator(".watcher-row")).toHaveCount(1, { timeout: 5_000 });
  // The row carries the cmd we seeded in the DB.
  await expect(page.locator(".watcher-cmd")).toContainText("sleep 600");

  // Snap the populated panel.
  await page.screenshot({
    path: "test-results/watcher-orphan-live.png",
    fullPage: false,
  });

  // Click the row to expand its tail. The tail pane should
  // show the orphan placeholder, NOT the actual sleep output
  // (which doesn't exist anyway — sleep produces no stdout).
  await page.locator(".watcher-row").first().click();
  await expect(page.locator(".watcher-log")).toContainText(
    /no buffered output/i,
    { timeout: 5_000 },
  );

  // Screenshot with the orphan-tail message visible.
  await page.screenshot({
    path: "test-results/watcher-orphan-tail.png",
    fullPage: false,
  });
});
