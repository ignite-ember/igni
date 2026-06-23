/**
 * End-to-end tests for the ``?demo=team`` chat sandbox.
 *
 * The demo (``src/dev/OrchestrateDemo.tsx``) renders every chat-item
 * kind, the orchestrate team-progress card, and the HitlDialog
 * approval flow through the real components but with hardcoded mock
 * data. That makes it the ideal target for browser tests: deterministic
 * (no backend, no network), exhaustive (every renderable surface is
 * present), and self-documenting (each scenario's title declares
 * what it's exercising).
 *
 * Tests are structured around three concerns:
 *
 *   1. Layout boot — header, nav, stage all mount.
 *   2. Scenario coverage — every nav button renders something
 *      distinctive (no silent regressions in any single scenario).
 *   3. Interactions — HITL allow/reject flow, tool-card expand,
 *      tool-window scroll containment.
 *
 * Backend-coupled flows (real chat send, streaming, file ops) need
 * their own fixture — out of scope here.
 */

import { test, expect, type Page } from "@playwright/test";

async function gotoDemo(page: Page) {
  await page.goto("/?demo=team");
  // Wait for the nav to mount — every scenario button shows up
  // at the same moment React renders.
  await expect(page.getByRole("heading", { name: /igni UI sandbox/i })).toBeVisible();
}

async function selectScenario(page: Page, title: string) {
  await page.getByRole("button", { name: title }).click();
  // The description paragraph updates on click — wait for the new
  // scenario's name to appear in the active button class so we don't
  // race the stage re-render.
  await expect(
    page.locator(".demo-nav-btn.active").filter({ hasText: title }),
  ).toBeVisible();
}

test.describe("demo page boot", () => {
  test("mounts header, nav, and stage", async ({ page }) => {
    await gotoDemo(page);
    await expect(page.locator(".demo-shell")).toBeVisible();
    await expect(page.locator(".demo-nav")).toBeVisible();
    await expect(page.locator(".demo-stage")).toBeVisible();
    // The default scenario (first one in the array) should be active
    // and its description text should be in the .demo-desc paragraph.
    const activeBtn = page.locator(".demo-nav-btn.active");
    await expect(activeBtn).toHaveCount(1);
  });

  test("every scenario button renders without error", async ({ page }) => {
    await gotoDemo(page);
    const buttons = page.locator(".demo-nav-btn");
    const count = await buttons.count();
    expect(count).toBeGreaterThanOrEqual(10);
    // Click through each scenario in order. The stage column should
    // always have at least one rendered child — empty would mean a
    // scenario broke its own rendering pipeline.
    for (let i = 0; i < count; i++) {
      await buttons.nth(i).click();
      await expect(page.locator(".demo-stage .col > *").first()).toBeVisible();
    }
  });
});

test.describe("orchestrate scenarios", () => {
  test("broadcast — three specialists, mid-flight", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Broadcast — three specialists, mid-flight");
    // The orchestrate card renders one row per started agent.
    await expect(page.locator(".orch-agent-name")).toHaveCount(3);
    await expect(page.locator(".orch-agent-name").nth(0)).toHaveText(/security/);
    await expect(page.locator(".orch-agent-name").nth(1)).toHaveText(/qa/);
    await expect(page.locator(".orch-agent-name").nth(2)).toHaveText(/reviewer/);
  });

  test("one specialist errored — error styling propagates", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Broadcast — one specialist errored");
    // The agent's status feeds a ``status-<status>`` class on both
    // the dot and the label (see ChatItems.tsx:506-508). We don't
    // pin to a count because the scenario evolves over time; just
    // confirm the surface exists.
    await expect(page.locator(".orch-agent-status.status-error").first()).toBeVisible();
  });

  test("paused-for-approval — agent shows paused status", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Sub-agent paused waiting for approval");
    await expect(page.locator(".orch-agent-status.status-paused").first()).toBeVisible();
  });
});

test.describe("stream scenarios", () => {
  test("edit tools — diff rows render with add/del classes", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Edit tools — replace, insert, multi-hunk, error");

    // The edit-tools scenario has four tool cards (3 successful edits
    // + 1 error). The error variant uses ``status === "error"`` which
    // styles the status dot differently.
    const toolCards = page.locator(".tool-card");
    await expect(toolCards.first()).toBeVisible();
    const cardCount = await toolCards.count();
    expect(cardCount).toBeGreaterThanOrEqual(4);

    // Click the first tool card to expand and reveal the diff table.
    await toolCards.first().locator(".tool-card-header").click();
    await expect(page.locator(".diff-table").first()).toBeVisible();
    // Diff rows are classed ``add`` / ``del`` based on the +/- prefix.
    await expect(page.locator(".diff-table .add").first()).toBeVisible();
    await expect(page.locator(".diff-table .del").first()).toBeVisible();
  });

  test("kitchen sink — every chat item kind is present", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Kitchen sink — every message kind");

    // Spot-check one representative selector per kind.
    await expect(page.locator(".msg-user").first()).toBeVisible();
    await expect(page.locator(".msg-assistant").first()).toBeVisible();
    await expect(page.locator(".tool-card").first()).toBeVisible();
    await expect(page.locator(".shell-output").first()).toBeVisible();
    await expect(page.locator(".loop-iteration").first()).toBeVisible();
    await expect(page.locator(".compact-card").first()).toBeVisible();
    await expect(page.locator(".msg-info").first()).toBeVisible();
    await expect(page.locator(".msg-error").first()).toBeVisible();
    await expect(page.locator(".thinking-toggle").first()).toBeVisible();
    await expect(page.locator(".agent-dispatch").first()).toBeVisible();
  });

  test("tool states — running, done, and error dots all render", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Tool states — running, done, error");
    await expect(page.locator(".tool-status.running")).toHaveCount(1);
    await expect(page.locator(".tool-status.error")).toHaveCount(1);
    // ``done`` is the largest set (two completed tools + the agent-
    // badged one); just confirm at least one exists.
    await expect(page.locator(".tool-status.done").first()).toBeVisible();
    // The ``rg`` call carries an ``agentName`` badge — the only
    // place that classname appears in the demo.
    await expect(page.locator(".tool-agent-badge")).toContainText("reviewer");
  });

  test("long thinking block — expand/collapse toggle works", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Long thinking block");
    const toggle = page.locator(".thinking-toggle").first();
    await expect(toggle).toBeVisible();
    // Body hidden until clicked.
    await expect(page.locator(".msg-thinking")).toHaveCount(0);
    await toggle.click();
    await expect(page.locator(".msg-thinking")).toBeVisible();
    await toggle.click();
    await expect(page.locator(".msg-thinking")).toHaveCount(0);
  });
});

test.describe("HITL approval flow", () => {
  test("single approval — dialog renders and step counter is absent", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "HITL — one approval needed");
    await expect(page.locator(".hitl-card")).toBeVisible();
    // Single requirement → no 1/N counter shown.
    await expect(page.locator(".hitl-card .dialog-title")).not.toContainText("/");
    // The four standard action buttons.
    await expect(page.getByRole("button", { name: "Allow once" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Always allow" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Allow similar" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();
  });

  test("batched approvals — step counter walks 1/3 → 2/3 → 3/3", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "HITL — three approvals batched");
    const title = page.locator(".hitl-card .dialog-title");
    await expect(title).toContainText("1/3");

    // Auto-dismiss the ``alert`` the demo's ``onResolve`` fires when
    // the batch finishes — Playwright pauses execution on dialogs
    // otherwise.
    page.on("dialog", (d) => d.dismiss());

    await page.getByRole("button", { name: "Allow once" }).click();
    await expect(title).toContainText("2/3");
    await page.getByRole("button", { name: "Allow once" }).click();
    await expect(title).toContainText("3/3");
  });

  test("team + HITL — orchestrate card + dialog coexist", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "Team — sub-agent paused for approval");
    // The team-progress card AND the HITL dialog should both be on
    // screen — the live UX is "team running, one branch paused".
    await expect(page.locator(".orchestrate-head")).toBeVisible();
    await expect(page.locator(".hitl-card")).toBeVisible();
    // The paused sub-agent shows up with the ``paused`` status.
    await expect(page.locator(".orch-agent-status.status-paused")).toHaveCount(1);
  });
});

test.describe("layout robustness", () => {
  test("wall-of-tools scenario keeps the agent card bounded", async ({ page }) => {
    await gotoDemo(page);
    await selectScenario(page, "One agent, 30 tool calls (scroll test)");
    // The agent is in ``running`` state, so its card auto-expands
    // on mount (ChatItems.tsx:469 initialises ``open`` to true for
    // running/paused agents). Don't click the head — that would
    // COLLAPSE it.
    const body = page.locator(".orch-agent-body").first();
    await expect(body).toBeVisible();
    // 30 tool cards inside the body — confirmed by count.
    await expect(body.locator(".tool-card")).toHaveCount(30);
    // The body is taller than its viewport — it must scroll
    // INTERNALLY (not push the parent), which means scrollHeight >
    // clientHeight on the body element.
    const overflows = await body.evaluate(
      (el: HTMLElement) => el.scrollHeight > el.clientHeight,
    );
    expect(overflows).toBeTruthy();
  });
});
