/**
 * Playwright config for end-to-end browser tests.
 *
 * Scope today: the ``?demo=team`` sandbox page (see
 * ``src/dev/OrchestrateDemo.tsx``). That page renders the chat
 * surface through the real components with hardcoded mock data,
 * so we can exercise every ChatItem kind, the HITL dialog flow,
 * and the orchestrate-card behaviour WITHOUT spawning a backend.
 *
 * The Vite dev server is auto-started for the test run via
 * ``webServer``; locally you'd otherwise have to ``npm run dev``
 * in another terminal. CI uses the same path.
 *
 * Backend-coupled flows (real chat send/receive, WS lifecycle,
 * file operations) need a fixture backend — separate suite when
 * we're ready.
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : "list",
  timeout: 20_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: "http://127.0.0.1:5179",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5179 --strictPort",
    url: "http://127.0.0.1:5179",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
