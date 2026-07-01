/**
 * Populated-state screenshot of the WatcherPanel.
 *
 * Uses the fixture BE (no real BE / no LLM needed). Scripts:
 *   - ``list_background_processes`` → 2 fake rows on open.
 *   - ``process_started`` push → a 3rd row appears live.
 *   - ``process_line`` pushes → a streaming tail populates the
 *     selected row's log pane.
 *   - ``process_exited`` push → flips one row to "stopped".
 *
 * Captures the panel after all of that lands so the screenshot
 * shows running, stopped, and streaming-tail at once.
 */
import { test, expect } from "./fixtures/embed";

test("WatcherPanel populated render", async ({ page, backend, appUrl }) => {
  // Seed the BE-side list with two running processes so the
  // panel opens already populated. The watcher's first action
  // is ``list_background_processes`` — answer it with our fixture.
  backend.onRpc("list_background_processes", () => [
    { pid: 8201, cmd: "pnpm run dev --host 127.0.0.1 --port 5179", elapsed_seconds: 47 },
    { pid: 8512, cmd: "tail -f /var/log/system.log", elapsed_seconds: 8 },
  ]);
  // When the user clicks a row we fetch its tail. Answer with
  // realistic-looking content for pid 8201 (the dev server).
  backend.onRpc("read_process_tail", (args) => {
    if (Number(args.pid) === 8201) {
      return {
        pid: 8201,
        output: [
          "",
          "  VITE v6.4.3  ready in 129 ms",
          "",
          "  ➜  Local:   http://127.0.0.1:5179/",
          "  ➜  press h + enter to show help",
          "",
          "9:33:14 PM [vite] hmr update /src/components/panels/WatcherPanel.tsx",
          "9:33:14 PM [vite] hmr update /src/App.tsx",
          "9:33:51 PM [vite] page reload src/theme.css",
        ].join("\n"),
        is_running: true,
        exit_code: null,
      };
    }
    return { pid: Number(args.pid), output: "", is_running: true, exit_code: null };
  });

  await page.goto(appUrl);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message Ember/,
  );

  // Open the panel by directly emitting a ``command_result`` push
  // for the watcher action — same shape the BE returns for
  // ``/watcher``. The fixture doesn't run a real BE command
  // handler, but the FE only needs the response envelope to
  // route through the action switch.
  await page.locator(".composer-editable").click();
  await page.locator(".composer-editable").type("/watcher");
  // We need to intercept the command request to reply with an
  // action result. The fixture's ``request()`` channel uses
  // correlated ids — wire a one-shot listener.
  await page.evaluate(() => {
    const orig = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data: string | ArrayBufferLike | Blob | ArrayBufferView) {
      try {
        const m = JSON.parse(String(data));
        if (m && m.type === "command" && String(m.text || "").trim() === "/watcher") {
          // Echo back a command_result that opens the panel.
          setTimeout(() => {
            const evt = new MessageEvent("message", {
              data: JSON.stringify({
                type: "command_result",
                id: m.id,
                kind: "action",
                content: "",
                action: "watcher",
              }),
            });
            (this as WebSocket).dispatchEvent(evt);
          }, 0);
        }
      } catch {
        /* not JSON */
      }
      return orig.call(this, data);
    };
  });
  await page.locator(".composer-editable").press("Enter");
  await expect(page.locator(".drawer")).toBeVisible();

  // Wait for the seed list to render.
  await expect(page.locator(".watcher-row")).toHaveCount(2);

  // Click pid 8201 to expand its tail.
  await page
    .locator(".watcher-row")
    .filter({ hasText: "8201" })
    .click();
  await expect(page.locator(".watcher-log")).toBeVisible();

  // Now stream a few extra lines on the push channel — they
  // should append to the tail in real time.
  for (const line of [
    "9:33:52 PM [vite] page reload src/components/panels/WatcherPanel.tsx",
    "9:33:53 PM [vite] hot updated: /src/theme.css",
    "9:33:54 PM [vite] hmr update /src/components/panels/WatcherPanel.tsx",
  ]) {
    backend.pushEvent({
      type: "push_notification",
      session_id: "test-session-001",
      channel: "process_line",
      payload: { pid: 8201, line },
    });
  }

  // A 3rd process starts mid-session — row should appear.
  backend.pushEvent({
    type: "push_notification",
    session_id: "test-session-001",
    channel: "process_started",
    payload: {
      pid: 9123,
      cmd: "python -m http.server 8765",
      started_at: Date.now() / 1000,
    },
  });
  // And a 4th process that exits immediately so we see the
  // "stopped" footer style too.
  backend.pushEvent({
    type: "push_notification",
    session_id: "test-session-001",
    channel: "process_started",
    payload: {
      pid: 9555,
      cmd: "cargo check --workspace",
      started_at: Date.now() / 1000 - 25,
    },
  });
  backend.pushEvent({
    type: "push_notification",
    session_id: "test-session-001",
    channel: "process_exited",
    payload: { pid: 9555, cmd: "cargo check --workspace", exit_code: 0 },
  });

  await expect(page.locator(".watcher-row")).toHaveCount(4);
  // Settle to let the streamed lines paint into the tail pane.
  await page.waitForTimeout(300);

  await page.screenshot({
    path: "test-results/watcher-populated.png",
    fullPage: false,
  });
});
