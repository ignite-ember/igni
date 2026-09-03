/**
 * Visual regression for the desktop app's main screens.
 *
 * E5, the same treatment the portal got in E4 and for the same reason:
 * 732 unit tests and 48 browser tests, none of which had ever looked
 * at a screen. The five defects E5 found were all found by looking.
 *
 * What makes the comparison mean anything here:
 *
 * **The data is already fixed.** Every screen is a `?demo=` route
 * driven from canned events in `src/dev/`, so there is no backend to
 * mock and no session id to vary.
 *
 * **The clock is pinned.** `WorkflowDemo` anchors its tape to
 * `Date.now() - 1h` so the card shows a sensible elapsed time rather
 * than "-3 years", which means the numbers move with the wall clock.
 * `setFixedTime` — not `install` — because the demos drive themselves
 * with timers, and a frozen timer queue would leave half of them
 * mid-render.
 *
 * **Only the Linux baselines are committed**, the platform CI runs on.
 * Two sets have to be regenerated in lockstep, and whoever regenerates
 * one leaves CI red for reasons unrelated to their change. See
 * `scripts/baselines-linux.sh`; on macOS these declare themselves
 * skipped and the run says which file is missing.
 */

import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test, expect } from '@playwright/test'
import { APP_SCREENS } from './app-screens'
import { openScreen } from './fixtures/screens'

const SNAPSHOTS = join(
  dirname(fileURLToPath(import.meta.url)),
  'app-visual.spec.ts-snapshots',
)

/** Fixed, so anything derived from the wall clock renders the same. */
const NOW = new Date('2026-09-03T12:00:00Z')

for (const theme of ['light', 'dark'] as const) {
  test.describe(`app visual — ${theme}`, () => {
    test.use({ viewport: { width: 1440, height: 900 } })

    for (const screen of APP_SCREENS) {
      const snapshot = `${screen.name}-${theme}`

      test(`${screen.name} @ ${theme}`, { tag: '@needs-baseline' }, async ({
        page,
      }) => {
        // Skipping a missing baseline is right when comparing and wrong
        // when generating — the portal's version of this gate refused
        // to let `--update-snapshots` create the file it was demanding
        // and reported the skips as success.
        const baseline = `${snapshot}-chromium-${process.platform}.png`
        const generating = test.info().config.updateSnapshots !== 'none'
        test.skip(
          !generating && !existsSync(join(SNAPSHOTS, baseline)),
          `no baseline for this platform: ${baseline} — see ` +
            `scripts/baselines-linux.sh`,
        )

        await page.clock.setFixedTime(NOW)
        const trouble = await openScreen(page, screen, theme)
        expect(trouble, trouble ?? '').toBeNull()

        await expect(page).toHaveScreenshot(`${snapshot}.png`, {
          fullPage: true,
          // Measured in the container these baselines come from, not
          // guessed: see the numbers in §83.
          maxDiffPixels: 100,
          animations: 'disabled',
          caret: 'hide',
        })
      })
    }
  })
}
