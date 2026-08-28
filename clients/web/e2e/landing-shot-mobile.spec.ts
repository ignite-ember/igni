/**
 * Phone-width product shot for the ignite-ember.sh landing page.
 *
 * The desktop capture (landing-shot.spec.ts) is 1920x1080; shown at
 * ~358px on a phone it scales to 19%, which makes the app's 14px UI
 * type render at under 3px. This is the portrait counterpart: far
 * fewer logical pixels, so the same type survives being displayed at
 * phone width, and a single short turn rather than two so the frame
 * is not wall-to-wall text.
 *
 * Run with:  npx playwright test e2e/landing-shot-mobile.spec.ts
 * Output:    LANDING_SHOT_DIR (defaults to ./landing-shots)
 */

import { test, expect } from "./fixtures/embed";

const OUT = process.env.LANDING_SHOT_DIR || "landing-shots";

const MODEL = "igni 1.0 27b";
const PROJECT = "/Users/dev/acme/payments-api";

// Narrow enough that the app lays itself out like a VSCode side panel,
// which is a real surface rather than a contrivance.
test.use({ viewport: { width: 460, height: 660 }, deviceScaleFactor: 2 });

test("capture phone product shot", async ({ page, backend, appUrl }) => {
  backend.onRpc("attach_session", () => ({
    session_id: "sess-landing",
    project_dir: PROJECT,
  }));
  backend.onRpc("get_session_id", () => "a1f9c2e4");
  backend.onRpc("get_status", () => ({
    type: "status_update",
    model: MODEL,
    context_tokens: 8_200,
    max_context: 200_000,
    cloud_connected: true,
    cloud_org: "Acme",
  }));
  backend.onRpc("count_context_tokens", () => 8_200);
  backend.onRpc("codeindex_status", () => ({
    install_state: "active",
    head_indexed: true,
    sync_in_progress: false,
    sync_error: null,
    remote_url: "https://github.com/acme/payments-api",
  }));
  backend.onRpc("get_login_state", () => ({ logged_in: true, email: "dev@acme.com" }));
  backend.onRpc("list_sessions", () => ({ sessions: [] }));

  await page.goto(appUrl);
  await expect(page.locator(".composer-editable")).toBeVisible();

  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.type("Which files touch both auth and billing?");
  await editor.press("Enter");

  await expect
    .poll(() => backend.received().find((m) => m.type === "user_message"))
    .toBeTruthy();
  const runId = String(backend.received().find((m) => m.type === "user_message")!.id);

  backend.pushEvent({ type: "run_started", id: runId });

  backend.pushEvent({
    type: "tool_started",
    id: "t1",
    run_id: runId,
    tool_name: "codeindex_search",
    friendly_name: "CodeIndex",
    args_summary: "domain:auth + domain:billing",
  });
  backend.pushEvent({
    type: "tool_completed",
    id: "t1",
    run_id: runId,
    summary: "3 findings across 5 files · 0.2s",
    full_result: "3 findings across 5 files · 0.2s",
    has_markup: false,
    diff_rows: null,
    is_error: false,
  });

  backend.pushEvent({
    type: "content_delta",
    id: runId,
    text:
      "Five files sit in both domains. Three carry risk:\n\n" +
      "**`checkout.py:47`** — imports `validate_session` but skips the role " +
      "check on plan downgrade.\n\n" +
      "**`stripe.py:82`** — no auth on the payment callback; it trusts the " +
      "Stripe signature alone.\n\n" +
      "**`billing.py:31`** — caches the resolved role past the token TTL.\n\n" +
      "Want me to patch all three?",
  });

  backend.pushEvent({ type: "stream_end", id: runId });
  backend.pushEvent({ type: "run_completed", id: runId });

  await expect(page.locator(".msg-assistant").last()).toContainText("patch all three");
  await page.waitForTimeout(1200);

  // Off-the-bottom scroll affordance is correct behaviour and reads as
  // an artefact in a still.
  await page.addStyleTag({ content: ".scroll-to-bottom { display: none !important; }" });
  await page.waitForTimeout(400);

  for (const scheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(500);
    await page.screenshot({
      path: `${OUT}/igni-app-mobile-${scheme}.png`,
      clip: { x: 0, y: 0, width: 460, height: 660 },
    });
  }
});
