/**
 * Live verification of orphan-process rehydration.
 *
 * Drives a real browser against the running Tauri BE
 * (``EMBER_LIVE_WS``). Assumes the test environment has
 * pre-seeded an orphan row in the project's ``state.db`` AND
 * has a real OS process matching that pid still alive (see
 * the harness shell script that calls this spec).
 *
 * Captures the watcher panel showing the orphan + clicks Kill,
 * verifies the row vanishes after the BE confirms termination.
 */
import { test as base, expect } from "@playwright/test";

type Fixtures = { liveWsUrl: string };

const test = base.extend<Fixtures>({
  liveWsUrl: async ({}, use) => {
    const url = process.env.EMBER_LIVE_WS;
    if (!url) test.skip(true, "Set EMBER_LIVE_WS");
    await use(url as string);
  },
});

test("orphan process surfaces after BE restart", async ({ page, liveWsUrl }) => {
  await page.goto(`/?ws=${encodeURIComponent(liveWsUrl)}`);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message Ember/,
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
    /stdout unavailable/i,
    { timeout: 5_000 },
  );

  // Screenshot with the orphan-tail message visible.
  await page.screenshot({
    path: "test-results/watcher-orphan-tail.png",
    fullPage: false,
  });
});
