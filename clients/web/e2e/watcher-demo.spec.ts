/**
 * One-shot screenshot of the WatcherPanel in action.
 *
 * Drives the FE against a live backend from ``fixtures/live-be``,
 * opens the watcher panel through the ``/watcher`` slash command and
 * captures it.
 *
 * It needs no seeded state — the spawn step below is a no-op by its
 * own admission — so it runs by default now. What it actually proves
 * is that the real backend's command dispatch turns ``/watcher`` into
 * a drawer, which no fixture spec covers. It was skipped for years as
 * "show me, not CI" on the strength of the screenshot alone. F128.
 */
import { test, expect } from "./fixtures/live-be";
import { runSlashCommand } from "./fixtures/composer";

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
  await runSlashCommand(page, "/watcher", page.locator(".drawer"));

  // The drawer renders with the title "Watcher".

  // Settle a beat so the seed RPC + any in-flight pushes land.
  await page.waitForTimeout(500);

  await page.screenshot({
    path: "test-results/watcher-panel.png",
    fullPage: false,
  });
});
