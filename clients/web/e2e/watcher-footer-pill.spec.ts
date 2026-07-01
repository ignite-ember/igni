/**
 * Screenshot of the footer indicator pill rendering when there's
 * at least one running background process. Drives a live BE
 * pre-seeded with an orphan row (via the harness script).
 */
import { test as base, expect } from "@playwright/test";

const test = base.extend<{ liveWsUrl: string }>({
  liveWsUrl: async ({}, use) => {
    const url = process.env.EMBER_LIVE_WS;
    if (!url) test.skip(true, "Set EMBER_LIVE_WS");
    await use(url as string);
  },
});

test("footer shows watcher pill when processes are running", async ({
  page,
  liveWsUrl,
}) => {
  await page.goto(`/?ws=${encodeURIComponent(liveWsUrl)}`);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message Ember/,
    { timeout: 30_000 },
  );

  // The pill is hidden when count === 0; visible otherwise. We
  // seeded one orphan before starting the BE, so the seed RPC
  // should report it back in the running list and the pill
  // appears.
  await expect(page.locator(".watcher-pill")).toBeVisible({ timeout: 5_000 });
  await expect(page.locator(".watcher-pill")).toContainText("1 running");

  // Screenshot the footer area.
  const footer = page.locator(".statusline");
  await footer.scrollIntoViewIfNeeded();
  const box = await footer.boundingBox();
  if (box) {
    await page.screenshot({
      path: "test-results/watcher-footer-pill.png",
      clip: {
        x: 0,
        y: Math.max(0, box.y - 20),
        width: page.viewportSize()!.width,
        height: box.height + 40,
      },
    });
  } else {
    await page.screenshot({ path: "test-results/watcher-footer-pill.png" });
  }

  // Click the pill — the panel opens.
  await page.locator(".watcher-pill").click();
  await expect(page.locator(".drawer")).toBeVisible();
  await page.screenshot({ path: "test-results/watcher-footer-pill-opened.png" });
});
