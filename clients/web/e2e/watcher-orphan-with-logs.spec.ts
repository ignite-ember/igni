/**
 * Cross-restart orphan with real stdout — the proper demo.
 *
 * The previous orphan demo showed the "no buffered output"
 * placeholder because the test harness used ``sleep`` (no
 * output). This one uses a process whose per-pid log file was
 * pre-seeded with real-looking dev-server output — the orphan's
 * ``read()`` should return that content verbatim.
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
    //     --scenario dev_server
    //   # start the backend against the repo root, then
    //   IGNI_LIVE_WS=ws://127.0.0.1:PORT npx playwright test e2e/watcher-orphan-with-logs.spec.ts
    //   .venv/bin/python scripts/seed_watcher_e2e_orphan.py --cleanup
    if (!url) test.skip(true, "needs a seeded orphan: see the header");
    await use(url as string);
  },
});

test("orphan tail pane shows real stdout from per-pid log", { tag: "@needs-seed" }, async ({
  page,
  liveWsUrl,
}) => {
  await page.goto(`/?ws=${encodeURIComponent(liveWsUrl)}`);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 30_000 },
  );

  // Footer pill should appear (1 running).
  await expect(page.locator(".watcher-pill")).toBeVisible({ timeout: 5_000 });

  // Click the pill → watcher opens with the orphan.
  await page.locator(".watcher-pill").click();
  await expect(page.locator(".watcher-row")).toHaveCount(1);

  // Click the row → tail pane loads from the per-pid log file.
  await page.locator(".watcher-row").click();
  await expect(page.locator(".watcher-log")).toContainText(
    "listening on http://127.0.0.1:3000",
    { timeout: 5_000 },
  );

  await page.screenshot({ path: "test-results/watcher-orphan-with-logs.png" });
});
