/**
 * Product-shot capture for the ignite-ember.sh landing page.
 *
 * Boots the REAL app (full chrome — sidebar, header, composer,
 * status footer) against the fixture WebSocket backend, scripts a
 * representative conversation over the wire, and captures the
 * window at 1920x1080 in both themes.
 *
 * Run with:  npx playwright test e2e/landing-shot.spec.ts
 * Output:    LANDING_SHOT_DIR (defaults to ./landing-shots)
 *
 * This is a capture harness, not a test — the only assertions are
 * the ones that keep it from screenshotting a half-rendered page.
 */

import { test, expect } from "./fixtures/embed";
import type { FixtureBackend } from "./fixtures/backend";

const OUT = process.env.LANDING_SHOT_DIR || "landing-shots";

// Logical viewport for the capture. The PNG comes out at
// LOGICAL x SCALE = 1920x1080; rendering at fewer logical pixels is
// what makes the type large enough to read once the landing page
// scales the image down to its column width.
const LOGICAL = { width: 1280, height: 720 };
const SCALE = 1.5;

// Phone variant. Far fewer logical pixels so the app's type is still
// legible once the landing page renders it ~358px wide, and portrait
// so it does not become a 40px-tall strip.
const NARROW = { width: 460, height: 900 };
const NARROW_SCALE = 2;

const MODEL = "igni 1.0 27b";
const PROJECT = "/Users/dev/acme/payments-api";

/** Status the header + footer read from. */
function status(contextTokens: number) {
  return {
    type: "status_update",
    model: MODEL,
    context_tokens: contextTokens,
    max_context: 200_000,
    cloud_connected: true,
    cloud_org: "Acme Engineering",
  };
}

/** Push a tool call that renders as a completed tool card. */
function tool(
  be: FixtureBackend,
  runId: string,
  id: string,
  toolName: string,
  friendly: string,
  args: string,
  summary: string,
  diffRows: [string, string][] | null = null,
) {
  be.pushEvent({
    type: "tool_started",
    id,
    run_id: runId,
    tool_name: toolName,
    friendly_name: friendly,
    args_summary: args,
  });
  be.pushEvent({
    type: "tool_completed",
    id,
    run_id: runId,
    summary,
    full_result: summary,
    has_markup: false,
    diff_rows: diffRows,
    is_error: false,
  });
}

test.use({ viewport: LOGICAL, deviceScaleFactor: SCALE });

test("capture landing product shot", async ({ page, backend, appUrl }) => {

  // Seat the session in a believable project before first paint so
  // the header shows a real folder rather than /tmp/test-project.
  backend.onRpc("attach_session", () => ({
    session_id: "sess-landing",
    project_dir: PROJECT,
  }));
  backend.onRpc("get_status", () => status(12_400));
  backend.onRpc("get_session_id", () => "a1f9c2e4");
  backend.onRpc("count_context_tokens", () => 12_400);
  // The footer pill polls ``codeindex_status`` (not the state RPC);
  // ``head_indexed`` is what classify() turns into the green
  // "indexed" badge — see CodeIndexIndicator.classify.
  backend.onRpc("codeindex_status", () => ({
    install_state: "active",
    head_indexed: true,
    sync_in_progress: false,
    sync_error: null,
    remote_url: "https://github.com/acme/payments-api",
  }));
  backend.onRpc("get_login_state", () => ({
    logged_in: true,
    email: "dev@acme.com",
  }));
  // The sidebar labels rows from ``name`` — ``session_id`` alone
  // renders as the raw id.
  backend.onRpc("list_sessions", () => ({
    sessions: [
      { session_id: "s1", name: "Trace the billing flow end to end" },
      { session_id: "s2", name: "Why is the webhook retrying twice?" },
      { session_id: "s3", name: "Add tests for auth/middleware.py" },
      { session_id: "s4", name: "Dead code under services/billing" },
    ],
  }));

  await page.goto(appUrl);
  await expect(page.locator(".composer-editable")).toBeVisible();

  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.type(
    "Which files touch both auth and billing? Any security risks?",
  );
  await editor.press("Enter");

  await expect
    .poll(() => backend.received().find((m) => m.type === "user_message"))
    .toBeTruthy();
  const runId = String(
    backend.received().find((m) => m.type === "user_message")!.id,
  );

  backend.pushEvent({ type: "run_started", id: runId });
  backend.pushEvent({
    type: "content_delta",
    id: runId,
    text:
      "Querying CodeIndex for entities tagged both `auth` and `billing`, " +
      "including tags inherited through imports.\n\n",
  });

  tool(
    backend,
    runId,
    "t1",
    "codeindex_search",
    "CodeIndex",
    "domain:auth + domain:billing + security:major-issues|minor-issues",
    "3 findings across 5 files (3 with auth inherited via imports) · 0.2s",
  );

  backend.pushEvent({
    type: "content_delta",
    id: runId,
    text:
      "Five files sit in both domains. Three carry risk:\n\n" +
      "**`services/checkout.py:47`** — imports `validate_session` but skips " +
      "the role check on plan downgrade, so a viewer-role user can move an " +
      "org onto a cheaper tier.\n\n" +
      "**`api/webhooks/stripe.py:82`** — no auth on the payment callback. " +
      "It trusts the Stripe signature alone, which is replayable if the " +
      "signing secret ever leaks.\n\n" +
      "**`services/billing.py:31`** — inherits `auth` via `get_current_user` " +
      "and caches the resolved role past the token TTL, so a revoked admin " +
      "keeps billing rights until the cache expires.\n\n",
  });

  tool(
    backend,
    runId,
    "t2",
    "read_file",
    "Read",
    "services/checkout.py:30-60",
    "31 lines",
  );

  backend.pushEvent({
    type: "content_delta",
    id: runId,
    text:
      "Confirmed on the first one — `downgrade_plan()` never calls " +
      "`require_role('admin')`, unlike every other mutating path in that " +
      "module.\n\nWant me to patch all three and add regression tests?",
  });

  backend.pushEvent({ type: "stream_end", id: runId });
  backend.pushEvent({ type: "run_completed", id: runId });
  backend.pushEvent(status(12_400));

  // ── Second turn: accept the fix, so the shot shows the agent
  //    actually editing code rather than just reporting. Also fills
  //    the 1080px height, which one Q&A turn leaves half empty.
  await editor.click();
  await editor.type("yes — patch all three, tests included");
  await editor.press("Enter");

  await expect
    .poll(() => backend.received().filter((m) => m.type === "user_message").length)
    .toBe(2);
  const run2 = String(
    backend.received().filter((m) => m.type === "user_message")[1].id,
  );

  backend.pushEvent({ type: "run_started", id: run2 });
  backend.pushEvent({
    type: "content_delta",
    id: run2,
    text: "Patching the role check first — it is the only one that is a plain omission.\n\n",
  });

  tool(
    backend,
    run2,
    "t3",
    "edit_file",
    "Edit",
    "services/checkout.py",
    "1 hunk · +4 −1",
    [
      ["  44   def downgrade_plan(org_id: str, tier: str) -> Plan:", "dim"],
      ["  45       session = validate_session(request)", "dim"],
      ["- 46       org = Org.get(org_id)", "del"],
      ["+ 46       require_role(session, \"admin\")", "add"],
      ["+ 47       org = Org.get(org_id)", "add"],
      ["+ 48       audit.record(\"plan.downgrade\", session.user_id, org_id)", "add"],
      ["  49       return org.set_tier(tier)", "dim"],
    ],
  );

  tool(
    backend,
    run2,
    "t4",
    "edit_file",
    "Edit",
    "api/webhooks/stripe.py",
    "1 hunk · +6 −0",
    [
      ["  80   async def stripe_webhook(request: Request) -> Response:", "dim"],
      ["  81       verify_stripe_signature(request)", "dim"],
      ["+ 82       event_id = request.json[\"id\"]", "add"],
      ["+ 83       if await replay_cache.seen(event_id):", "add"],
      ["+ 84           return Response(status=200)  # idempotent no-op", "add"],
      ["+ 85       await replay_cache.mark(event_id)", "add"],
    ],
  );

  tool(
    backend,
    run2,
    "t5",
    "run_shell_command",
    "Bash",
    "pytest tests/test_checkout.py tests/test_webhooks.py -q",
    "14 passed in 2.31s",
  );

  backend.pushEvent({
    type: "content_delta",
    id: run2,
    text:
      "All three patched, 14 tests green:\n\n" +
      "- `checkout.py` requires `admin` and writes an audit row\n" +
      "- `stripe.py` dedupes on the Stripe event id\n" +
      "- `billing.py` role cache now tracks token expiry\n\n" +
      "Want me to open a PR?",
  });

  backend.pushEvent({ type: "stream_end", id: run2 });
  backend.pushEvent({ type: "run_completed", id: run2 });
  backend.pushEvent(status(31_800));
  backend.onRpc("count_context_tokens", () => 31_800);

  // Let markdown, highlighting, and the tool cards settle.
  await expect(page.locator(".msg-assistant").last()).toContainText("open a PR");
  await expect(page.locator(".tool-card")).toHaveCount(5);

  // Expand the first Edit card so the diff table is on screen — a
  // collapsed row shows the filename but not the work.
  await page.locator(".tool-card").nth(2).locator(".tool-card-header").click();
  await expect(page.locator(".diff-table").first()).toBeVisible();

  // Settle at the bottom of the transcript. Mid-scroll leaves a
  // half-clipped card at the top edge and keeps the "jump to
  // latest" chevron floating over the copy — both read as glitches
  // in a still image.
  await page.mouse.move(640, 360);
  await page.mouse.wheel(0, 4000);
  await page.waitForTimeout(800);
  // Back off the bottom by a little so the user's prompt bubble is
  // in frame. The tail of the reply slides under the composer, which
  // is how the app looks in normal use anyway — the composer floats
  // over the transcript.
  await page.mouse.wheel(0, -185);
  await expect(page.locator(".msg-user").last()).toBeInViewport();

  // Off the bottom, the app shows its "jump to latest" affordance.
  // It is correct behaviour and wrong for a still image — it floats
  // over the transcript and reads as a stray artifact. Hide it for
  // the capture only.
  await page.addStyleTag({ content: ".scroll-to-bottom { display: none !important; }" });
  await page.waitForTimeout(1200);

  for (const scheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(600);
    await page.screenshot({
      path: `${OUT}/igni-app-${scheme}.png`,
      clip: { x: 0, y: 0, ...LOGICAL },
    });
  }
});
