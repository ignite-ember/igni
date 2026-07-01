/**
 * One-shot demo: drives the orchestrate demo page, posts a chat
 * item containing a fenced ASCII-art block, screenshots it to
 * prove svgbob is gone — the diagram should render as plain
 * code, NOT as an SVG.
 *
 * Not meant to live in CI long-term; this is a "show me" check.
 */
import { test, expect } from "@playwright/test";

test.use({ viewport: { width: 1280, height: 1100 } });

test("ASCII art in a fenced block renders as plain code, not SVG", async ({
  page,
}) => {
  await page.goto("/?demo=team");
  await page.waitForLoadState("networkidle");

  // Switch to the markdown-rendering scenario — that's the demo
  // tab that used to contain the svgbob example blocks.
  await page
    .getByRole("button", { name: /Markdown rendering — every block type/ })
    .click();
  await page.waitForTimeout(400);

  expect(await page.locator(".svgbob-diagram").count()).toBe(0);
  expect(await page.locator("[class*='svgbob']").count()).toBe(0);
  expect(await page.locator(".code-block-wrap").count()).toBeGreaterThan(0);

  // Body has ``overflow: hidden`` per the dev sandbox CSS;
  // ``.demo-shell`` is the actual scroll container (see
  // ``theme.css::.demo-shell``). Walk it top-to-bottom, snap
  // a viewport-height frame at each scroll position.
  const shell = page.locator(".demo-shell");
  await shell.waitFor({ state: "visible" });

  const sliceCount = 12;
  let prevTop = -1;
  for (let i = 0; i < sliceCount; i++) {
    await page.screenshot({
      path: `test-results/svgbob-removed-slice-${i + 1}.png`,
    });
    const state = (await shell.evaluate((el: HTMLElement) => ({
      top: el.scrollTop,
      max: el.scrollHeight - el.clientHeight,
      h: el.clientHeight,
    }))) as { top: number; max: number; h: number };
    if (state.top >= state.max - 1 || state.top === prevTop) break;
    prevTop = state.top;
    await shell.evaluate(
      (el: HTMLElement, by: number) => el.scrollBy({ top: by, behavior: "auto" }),
      Math.max(500, state.h - 80),
    );
    await page.waitForTimeout(160);
  }
});
