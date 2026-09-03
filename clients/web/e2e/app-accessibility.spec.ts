/**
 * Every main screen, checked by axe.
 *
 * E6. The portal's accessibility dependency was declared and never
 * invoked (F68); this client had no such dependency at all, and the
 * first run over these seventeen screens found **154 violation
 * nodes** — 124 of them colour contrast. They are all fixed (F138);
 * this is the gate that keeps them fixed.
 *
 * Dark theme, because that is where the app's own palette lives and
 * where the failures were. The light theme's `--fg-faint` was worse
 * (2.57:1 against 3.08), and the token fix covers both — a per-theme
 * sweep is `IGNI_E2E_A11Y_BOTH=1`.
 */

import { test, expect } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { APP_SCREENS } from './app-screens'
import { openScreen } from './fixtures/screens'

/** WCAG 2.0/2.1 A and AA, plus axe's best-practice set. */
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']

const THEMES = process.env.IGNI_E2E_A11Y_BOTH === '1'
  ? (['dark', 'light'] as const)
  : (['dark'] as const)

for (const theme of THEMES) {
  test.describe(`accessibility — ${theme}`, () => {
    test.use({ viewport: { width: 1440, height: 900 } })

    for (const screen of APP_SCREENS) {
      test(`${screen.name} has no axe violations`, async ({ page }) => {
        const trouble = await openScreen(page, screen, theme)
        expect(trouble, trouble ?? '').toBeNull()

        const results = await new AxeBuilder({ page }).withTags(TAGS).analyze()

        // A screen that rendered nothing passes every rule. Assert the
        // scan had something to measure — this is how the earlier runs
        // reported a clean zero while the build was broken and the page
        // was empty.
        const measured = results.passes.reduce((n, p) => n + p.nodes.length, 0)
        expect(measured, `${screen.name}: axe checked almost nothing`).toBeGreaterThan(20)

        const summary = results.violations.map(
          (v) =>
            `${v.id} (${v.impact}, ${v.nodes.length}×): ${v.help}\n` +
            v.nodes
              .slice(0, 5)
              .map((n) => `    ${n.target.join(' ')}\n      ${n.failureSummary?.split('\n')[1] ?? ''}`)
              .join('\n'),
        )
        expect(summary, summary.join('\n\n')).toEqual([])
      })
    }
  })
}
