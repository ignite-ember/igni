/**
 * PlanCard end-to-end contract — guards the bug the user hit:
 * a plan rendered, but the footer text flipped to "Plan approved —
 * plan mode exited" without anyone clicking the Approve button.
 *
 * The architectural fix moved plan state out of inferred-from-mode
 * into BE-persisted decisions. These specs prove the FE side of
 * that contract from a real browser:
 *
 *   1. ``plan_submitted`` push renders a PlanCard with the
 *      ``--pending`` buttons visible (NOT a footer text).
 *   2. Clicking Approve fires ``approve_plan(run_id)`` over the
 *      wire — not just a local state mutation.
 *   3. The card only flips visual state when the BE confirms via
 *      a ``plan_decided`` push. No optimistic local flip.
 *   4. Refine fires ``dismiss_plan(run_id)`` and waits on the
 *      same push channel.
 *
 * Without (3), a future regression that pre-flips the local
 * state on click — like the original FE — would still pass
 * "the card flipped" smoke tests. The check on inbound RPCs is
 * what catches the *wrong source of truth* bug.
 */

import { test, expect } from "./fixtures/embed";

const PLAN_TEXT = "## Refactor _mode_step\n\nSplit into 4 helpers.";
const RUN_ID = "run-xyz-7";
const SESSION_ID = "test-session-001";

test.describe("PlanCard ←→ BE contract", () => {
  test("plan_submitted push renders an unflipped, pending card with buttons", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );

    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: {
        plan: PLAN_TEXT,
        tasks: [
          { content: "Extract _tool_category()", status: "pending" },
          { content: "Extract _eval_plan_mode()", status: "pending" },
        ],
        run_id: RUN_ID,
      },
    });

    // Card body renders the plan markdown.
    await expect(page.locator(".plan-card-body")).toContainText(
      "Refactor _mode_step",
    );
    // Buttons MUST be visible — this is the contract we broke
    // before. ``plan-card-footer`` (the "Plan approved" / "Plan
    // dismissed" text) is the OTHER branch and must NOT appear
    // until the BE confirms a decision.
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toBeVisible();
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--reject"),
    ).toBeVisible();
    await expect(page.locator(".plan-card-footer")).toHaveCount(0);
  });

  test("clicking Approve fires approve_plan(run_id) RPC", async ({
    page,
    backend,
    appUrl,
  }) => {
    // Stub the RPC handler so the BE-side promise resolves with a
    // shape the FE can read. Without this, the unhandled-RPC fast
    // failure would surface as a console error and the FE would
    // never know its click made it across.
    backend.onRpc("approve_plan", (args) => ({
      run_id: String(args.run_id ?? ""),
      decision: "approved",
      mode_status: "Permission mode: plan → default.",
    }));

    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: { plan: PLAN_TEXT, tasks: [], run_id: RUN_ID },
    });
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toBeVisible();

    await page
      .locator(".plan-card-btn.plan-card-btn--approve")
      .click();

    // The wire-level contract. The whole point of the refactor is
    // that the FE no longer mutates state locally — it asks the
    // BE to record the decision, keyed by run_id.
    await expect
      .poll(() =>
        backend.received().some(
          (m) =>
            m.type === "rpc_request" &&
            m.method === "approve_plan" &&
            (m.args as { run_id?: string })?.run_id === RUN_ID,
        ),
      )
      .toBe(true);
  });

  test("card does NOT flip on click alone — only on plan_decided push", async ({
    page,
    backend,
    appUrl,
  }) => {
    // The headline regression. Pre-fix, ``onApprovePlan`` set
    // ``state: "approved"`` locally, so the footer flipped
    // BEFORE the BE knew. That's how the "I've never approved
    // it" symptom slipped in (any rehydration or stale state
    // could trigger the same flip with no user action).
    //
    // We delay the BE's response and assert the card stays in
    // the buttons-visible state during the gap.
    let resolveRpc: ((value: unknown) => void) | null = null;
    backend.onRpc("approve_plan", (args) => {
      // Returning a promise from the handler makes the fixture
      // wait — but RpcHandler is sync. So instead, we hold off
      // the ``plan_decided`` push and observe the card during
      // the window. The ``approve_plan`` response itself is
      // immediate, but the FE's visual flip is gated on the
      // push, not on the RPC ack — that's the design we're
      // pinning.
      void resolveRpc;
      return {
        run_id: String(args.run_id ?? ""),
        decision: "approved",
        mode_status: "",
      };
    });

    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: { plan: PLAN_TEXT, tasks: [], run_id: RUN_ID },
    });
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toBeVisible();

    await page
      .locator(".plan-card-btn.plan-card-btn--approve")
      .click();

    // Wait for the RPC to land — proves the click made it
    // across — but DON'T send ``plan_decided`` yet.
    await expect
      .poll(() =>
        backend.received().some(
          (m) => m.type === "rpc_request" && m.method === "approve_plan",
        ),
      )
      .toBe(true);

    // Card still shows buttons — the FE didn't pre-flip locally.
    // 250ms is enough for any synchronous state mutation to land
    // in React's commit phase; if a regression re-introduces a
    // local optimistic flip this assertion fails immediately.
    await page.waitForTimeout(250);
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toBeVisible();
    await expect(page.locator(".plan-card-footer")).toHaveCount(0);

    // NOW the BE confirms — push ``plan_decided`` and watch the
    // card flip to the approved-footer state.
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_decided",
      payload: { run_id: RUN_ID, decision: "approved" },
    });

    await expect(page.locator(".plan-card-footer--approved")).toBeVisible();
    await expect(page.locator(".plan-card-footer--approved")).toContainText(
      "Plan approved",
    );
    // And the buttons are gone (mutual exclusion at the render
    // layer — see ChatItems.tsx::PlanCard).
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toHaveCount(0);
  });

  test("clicking Refine fires dismiss_plan and flips on plan_decided", async ({
    page,
    backend,
    appUrl,
  }) => {
    backend.onRpc("dismiss_plan", (args) => ({
      run_id: String(args.run_id ?? ""),
      decision: "dismissed",
      mode_status: "",
    }));

    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: { plan: PLAN_TEXT, tasks: [], run_id: RUN_ID },
    });
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--reject"),
    ).toBeVisible();

    await page
      .locator(".plan-card-btn.plan-card-btn--reject")
      .click();

    await expect
      .poll(() =>
        backend.received().some(
          (m) =>
            m.type === "rpc_request" &&
            m.method === "dismiss_plan" &&
            (m.args as { run_id?: string })?.run_id === RUN_ID,
        ),
      )
      .toBe(true);

    // Same as approve — visual flip waits on the push.
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_decided",
      payload: { run_id: RUN_ID, decision: "dismissed" },
    });

    await expect(page.locator(".plan-card-footer--dismissed")).toBeVisible();
    await expect(page.locator(".plan-card-footer--dismissed")).toContainText(
      "Plan dismissed",
    );
  });

  test("plan_decided routes to the right card by run_id", async ({
    page,
    backend,
    appUrl,
  }) => {
    // Two plans stacked in the chat. Approving one must not
    // affect the other. This catches an "approve all by kind"
    // regression — e.g. the FE forgetting to key updates by
    // run_id and matching every plan ChatItem.
    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
    );

    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: { plan: "First plan", tasks: [], run_id: "run-A" },
    });
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_submitted",
      payload: { plan: "Second plan", tasks: [], run_id: "run-B" },
    });

    // Both cards rendered with buttons.
    await expect(page.locator(".plan-card-body")).toHaveCount(2);
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toHaveCount(2);

    // Confirm only run-A.
    backend.pushEvent({
      type: "push_notification",
      session_id: SESSION_ID,
      channel: "plan_decided",
      payload: { run_id: "run-A", decision: "approved" },
    });

    // Exactly ONE approved footer; one card still has its
    // buttons (run-B is untouched).
    await expect(page.locator(".plan-card-footer--approved")).toHaveCount(1);
    await expect(
      page.locator(".plan-card-btn.plan-card-btn--approve"),
    ).toHaveCount(1);
  });
});
