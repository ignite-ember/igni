/**
 * Playwright ``test.extend`` wrapper that mounts a fresh fixture
 * backend per test and exposes it as ``backend`` in the test
 * arguments. Tear-down happens automatically when the test exits.
 *
 * Use this instead of the bare ``test`` import in any spec that
 * exercises the real web app (vs. the ``?demo=team`` sandbox,
 * which has no backend at all).
 */

import { test as base } from "@playwright/test";
import {
  startFixtureBackend,
  type FixtureBackend,
  type RpcHandler,
  type Envelope,
  type StartOptions,
} from "./backend";

type Fixtures = {
  backend: FixtureBackend;
  /** Helper that navigates to the app pointing at this test's
   *  fixture backend. Wraps ``page.goto`` so the ``?ws=`` param
   *  doesn't have to be threaded through every test. */
  appUrl: string;
};

export const test = base.extend<Fixtures>({
  backend: async ({}, use) => {
    const be = await startFixtureBackend();
    await use(be);
    await be.close();
  },
  appUrl: async ({ backend }, use) => {
    // The FE picks the WS URL from the ``?ws=`` query param
    // (``protocol/client.ts::resolveWsUrl``); deliver the fixture
    // port that way so the test doesn't have to inject scripts.
    await use(`/?ws=${encodeURIComponent(backend.url)}`);
  },
});

export { expect } from "@playwright/test";
export { startFixtureBackend } from "./backend";
export type { FixtureBackend, RpcHandler, Envelope, StartOptions };
