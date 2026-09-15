/**
 * A picture of every main screen, in both themes, for looking at.
 *
 * E5. Writes a PNG per screen per theme so the layout can be reviewed.
 * Asserts almost nothing on purpose — a screenshot generator that also
 * polices layout fails for two different reasons and teaches people to
 * ignore both. Pixel comparison is `app-visual.spec.ts`.
 *
 *   IGNI_E2E_SHOTS=1 npx playwright test app-shots
 *
 * Output lands in `shots/<theme>/`, gitignored: these are evidence of a
 * review, not an artefact.
 *
 * The theme comes from `localStorage['ember:theme']` rather than from
 * the browser's `prefers-color-scheme`. `ThemeToggle` cycles
 * auto → light → dark and writes that key, and in `auto` it mirrors the
 * OS preference into a second attribute — so setting the key states
 * exactly what is being pictured, where driving the media query would
 * be picturing "auto, on a machine that happens to prefer dark".
 */

import { test, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { APP_SCREENS } from './app-screens'
import { openScreen } from './fixtures/screens'

for (const theme of ['light', 'dark'] as const) {
  test.describe(`${theme} theme`, () => {
    test.use({ viewport: { width: 1440, height: 900 } })

    test(`every main screen, ${theme}`, { tag: '@manual' }, async ({ page }) => {
      test.skip(
        process.env.IGNI_E2E_SHOTS !== '1',
        'Set IGNI_E2E_SHOTS=1 to write screenshots. This produces files ' +
          'for a human to review and asserts almost nothing, so it is ' +
          'not a gate.',
      )
      test.setTimeout(180_000)

      const dir = `shots/${theme}`
      mkdirSync(dir, { recursive: true })

      const problems: string[] = []
      for (const screen of APP_SCREENS) {
        const trouble = await openScreen(page, screen, theme)
        if (trouble) {
          problems.push(trouble)
          continue
        }
        await page.screenshot({ path: `${dir}/${screen.name}.png`, fullPage: true })
        await page.screenshot({
          path: `${dir}/${screen.name}-fold.png`,
          fullPage: false,
        })
      }

      expect(problems, problems.join('\n')).toEqual([])
    })
  })
}
