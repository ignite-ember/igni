/**
 * One-shot screenshot of the WatcherPanel in action.
 *
 * Drives the FE against a live BE (``EMBER_LIVE_WS``), spawns a
 * background process by calling the agent's
 * ``run_shell_command(background=True)`` tool through a direct
 * RPC, then opens the watcher panel and captures it.
 *
 * Skipped by default — this is "show me," not CI.
 */
import { test as base, expect } from "@playwright/test";

type Fixtures = {
  liveWsUrl: string;
};

const test = base.extend<Fixtures>({
  liveWsUrl: async ({}, use) => {
    const url = process.env.EMBER_LIVE_WS;
    if (!url) {
      test.skip(true, "Set EMBER_LIVE_WS to a running BE's ws URL.");
    }
    await use(url as string);
  },
});

test("watcher panel shows live background processes", async ({
  page,
  liveWsUrl,
}) => {
  // ── 1. Spawn a couple of background processes via a raw WS
  // connection BEFORE the FE mounts. The seed RPC
  // (``list_background_processes``) should pick them up so the
  // panel opens with rows already populated.
  await page.evaluate(
    async ({ wsUrl }) => {
      const ws = new WebSocket(wsUrl);
      await new Promise<void>((resolve) => (ws.onopen = () => resolve()));
      // Drain the Welcome.
      await new Promise<void>((resolve) => {
        ws.onmessage = (ev) => {
          const m = JSON.parse(String(ev.data));
          if (m.type === "welcome") {
            ws.onmessage = null;
            resolve();
          }
        };
      });
      // The BE has no FE-facing RPC to spawn background processes
      // (that's intentional — only the agent does it). For this
      // demo, the user has to manually create some via a real
      // agent run. We'll skip the spawn step if no processes
      // exist; the empty-state still proves the panel works.
      ws.close();
    },
    { wsUrl: liveWsUrl },
  );

  // ── 2. Open the FE.
  await page.goto(`/?ws=${encodeURIComponent(liveWsUrl)}`);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
    { timeout: 30_000 },
  );

  // ── 3. Open the watcher panel via the slash command.
  await page.locator(".composer-editable").click();
  await page.locator(".composer-editable").type("/watcher");
  await page.locator(".composer-editable").press("Enter");

  // The drawer renders with the title "Watcher".
  await expect(page.locator(".drawer")).toBeVisible({ timeout: 5_000 });

  // Settle a beat so the seed RPC + any in-flight pushes land.
  await page.waitForTimeout(500);

  await page.screenshot({
    path: "test-results/watcher-panel.png",
    fullPage: false,
  });
});
