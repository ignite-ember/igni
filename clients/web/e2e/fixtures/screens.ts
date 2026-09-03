/**
 * Open one of the app's main screens, in a stated theme, and settle it.
 *
 * E5. Shared by the shot generator and the visual baselines so the two
 * cannot picture different things — the reason the portal's baselines
 * ended up on a page nobody looked at was that its capture and its
 * comparison had drifted apart.
 */

import { expect, type Page } from '@playwright/test'
import type { AppScreen } from '../app-screens'

/**
 * Returns null when the screen is ready, or a description of what went
 * wrong — the caller collects them so one bad screen does not hide the
 * rest.
 */
export async function openScreen(
  page: Page,
  screen: AppScreen,
  theme: 'light' | 'dark',
): Promise<string | null> {
  // Applied before the first paint: `ThemeToggle` reads this key on
  // import precisely so the first render already has the right palette.
  await page.addInitScript(
    (value) => window.localStorage.setItem('ember:theme', value),
    theme,
  )

  await page.goto(`/?demo=${screen.demo}`)
  await page.waitForLoadState('networkidle')

  if (screen.scenario) {
    const button = page.getByRole('button', { name: screen.scenario })
    try {
      await button.waitFor({ timeout: 15_000 })
      await button.click()
    } catch {
      return `${screen.name}: no scenario button matching ${screen.scenario}`
    }
  }

  if (screen.click) {
    const control = page.getByRole('button', { name: screen.click })
    try {
      await control.waitFor({ timeout: 10_000 })
      await control.click()
    } catch {
      return `${screen.name}: no control matching ${screen.click}`
    }
  }

  if (screen.expandToolCards) {
    const headers = page.locator('.tool-card .tool-card-header')
    const count = await headers.count()
    if (count === 0) return `${screen.name}: asked to expand tool cards, found none`
    for (let i = 0; i < count; i++) {
      await headers.nth(i).click()
    }
    // At least one has to have opened, or the shot is of the collapsed
    // state and looks deliberate.
    await expect(page.locator('.diff-table, .tool-card-body').first()).toBeVisible({
      timeout: 5_000,
    })
  }

  if (screen.ready) {
    const want = screen.ready.count ?? 1
    const locator = page.locator(screen.ready.selector)
    try {
      await expect
        .poll(async () => locator.count(), { timeout: 30_000, intervals: [100, 250, 500] })
        .toBeGreaterThanOrEqual(want)
    } catch {
      const found = await locator.count()
      return (
        `${screen.name}: waited for ${want}×${screen.ready.selector}, ` +
        `found ${found}`
      )
    }
  }

  // A short settle on top, for web fonts and the last layout pass.
  // Everything structural is waited for above.
  await page.waitForTimeout(screen.settleMs ?? 900)

  const applied = await page.evaluate(() => document.documentElement.dataset.theme)
  if (applied !== theme) {
    return `${screen.name}: asked for ${theme}, page is in ${applied}`
  }

  // Something has to be on the page. A demo route that silently renders
  // nothing would otherwise produce a baseline of an empty div — which
  // is how the portal came to have two baselines of a page with no data
  // on it (F67).
  const rendered = await page.evaluate(
    () => (document.body.innerText ?? '').trim().length,
  )
  if (rendered < 40) return `${screen.name}: rendered almost no text`

  // Animations off, so a spinner mid-rotation is not the difference
  // between two runs.
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
    }`,
  })
  await expect(page.locator('body')).toBeVisible()
  return null
}
