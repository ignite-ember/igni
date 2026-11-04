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

/**
 * Every timeout below, multiplied. 1 unless a run says otherwise —
 * CI does not. `scripts/baselines-linux.sh` sets it: there the browser
 * runs under amd64 emulation on an arm64 Mac, several times slower,
 * and the defaults timed tests out that were only slow (T18).
 */
const TIMEOUT_FACTOR = Number(process.env.IGNI_E2E_TIMEOUT_FACTOR ?? 1);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  // ``reporters/skips.ts`` is not optional decoration: without it a run
  // that skips every integration test still ends in "41 passed". F128.
  reporter: [
    [process.env.CI ? "github" : "list"],
    ["./e2e/reporters/skips.ts"],
  ],
  timeout: 20_000 * TIMEOUT_FACTOR,
  // A missing baseline must never write itself. Playwright's default
  // ('missing') would have a runner *create* the baseline it has none
  // of and then pass — a visual suite that approves whatever it first
  // sees. Generating is deliberate: `--update-snapshots`. E5.
  updateSnapshots: 'none',
  expect: { timeout: 5_000 * TIMEOUT_FACTOR },

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
    timeout: 60_000 * TIMEOUT_FACTOR,
    stdout: "ignore",
    stderr: "pipe",
  },
});
