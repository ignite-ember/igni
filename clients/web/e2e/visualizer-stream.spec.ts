/**
 * End-to-end proof: the client-side visualizer streaming pipeline
 * actually renders a chart when fed progressive JSON deltas.
 *
 * Runs the ``?demo=viz-stream`` sandbox — the smallest possible
 * app that exercises ``applyVisualizationDelta`` + JsonRenderView
 * WITHOUT any BE, WebSocket, or agent. If this test passes AND the
 * real chat still doesn't render, the bug is between the BE and the
 * FE's ``visualization_delta`` handler, NOT the renderer.
 *
 * Screenshots are saved so a human can visually diff future
 * regressions: ``test-results/viz-stream-<name>.png``.
 */

import { test, expect, type Page } from "@playwright/test";

const DEMO_URL = "/?demo=viz-stream";

async function gotoDemo(page: Page) {
  await page.goto(DEMO_URL);
  await expect(page.getByRole("heading", { name: /visualizer streaming/i })).toBeVisible();
  await expect(page.getByTestId("stream-empty")).toBeVisible();
}

test.describe("viz-stream demo — client pipeline sanity", () => {
  test("one-shot: full spec renders as a Card + LineGraph", async ({ page }) => {
    await gotoDemo(page);
    await page.getByTestId("stream-one").click();

    // Status jumps straight to done + 100% progress.
    const status = page.getByTestId("stream-status");
    await expect(status).toHaveAttribute("data-status", "done");
    await expect(status).toHaveAttribute("data-progress", "100");

    // Exactly one visualization card should exist.
    await expect(page.getByTestId("stream-body")).toHaveAttribute(
      "data-card-count",
      "1",
    );

    // The Card header from the AAPL spec is present.
    await expect(page.getByText("AAPL — Monthly Close")).toBeVisible();
    await expect(page.getByText("2023")).toBeVisible();

    // LineGraph is a real SVG with data points (12 monthly closes).
    const dots = page.locator(".jr-linechart-dot");
    await expect(dots).toHaveCount(12);
    // The polyline (the line itself) exists too.
    await expect(page.locator(".jr-linechart-line")).toBeVisible();

    await page.screenshot({
      path: "test-results/viz-stream-oneshot.png",
      fullPage: true,
    });
  });

  test("streaming: chart fills in progressively over multiple deltas", async ({
    page,
  }) => {
    await gotoDemo(page);
    await page.getByTestId("stream-start").click();

    const status = page.getByTestId("stream-status");

    // The stream is running — status flips to "streaming" immediately.
    await expect(status).toHaveAttribute("data-status", "streaming");

    // Wait until progress is at least 20% — that's after the ``{root}``
    // portion but before the full data array. A card should be
    // present at this point (the progressive parser has locked on).
    await expect
      .poll(async () => {
        const raw = await status.getAttribute("data-progress");
        return Number(raw ?? "0");
      })
      .toBeGreaterThanOrEqual(20);
    await expect(page.getByTestId("stream-body")).toHaveAttribute(
      "data-card-count",
      "1",
    );
    await page.screenshot({
      path: "test-results/viz-stream-partial-20pct.png",
      fullPage: true,
    });

    // Wait for a mid-stream snapshot around 60% — the chart has
    // some data now but the model may still be typing the rest.
    await expect
      .poll(async () => {
        const raw = await status.getAttribute("data-progress");
        return Number(raw ?? "0");
      })
      .toBeGreaterThanOrEqual(60);
    await page.screenshot({
      path: "test-results/viz-stream-partial-60pct.png",
      fullPage: true,
    });

    // Wait for the stream to complete.
    await expect(status).toHaveAttribute("data-status", "done", {
      timeout: 10_000,
    });
    await expect(status).toHaveAttribute("data-progress", "100");

    // Final state: card present with all 12 dots.
    await expect(page.locator(".jr-linechart-dot")).toHaveCount(12);
    await expect(page.getByText("AAPL — Monthly Close")).toBeVisible();

    await page.screenshot({
      path: "test-results/viz-stream-final.png",
      fullPage: true,
    });
  });

  test("reset clears the card and status", async ({ page }) => {
    await gotoDemo(page);
    await page.getByTestId("stream-one").click();
    await expect(page.getByTestId("stream-body")).toHaveAttribute(
      "data-card-count",
      "1",
    );
    await page.getByTestId("stream-reset").click();
    await expect(page.getByTestId("stream-status")).toHaveAttribute(
      "data-status",
      "idle",
    );
    await expect(page.getByTestId("stream-empty")).toBeVisible();
  });
});
