/**
 * Product shots for the landing page's Extensions section.
 *
 * An insurer's claims team, three ways — all captured from the real
 * client against the fixture backend, in both themes:
 *
 *   1. `igni-claims-plain-*`  — igni with nothing installed: a claims
 *      handler asks about a claim, attaches a photo, the agent reads
 *      the file and answers in plain language.
 *   2. `igni-claims-app-*`    — the same igni with "Claims desk"
 *      (`acme.claims`) installed and its page open. The page is a real
 *      extension bundle served from its own origin and framed by the
 *      client's `ExtensionFrame` (see `fixtures/claims-desk.ts`).
 *   3. `igni-claims-chat-*`   — the chat with the app installed: the
 *      agent calls the app's tools, the app posts a message as itself
 *      (`host.chat.post`) and appends a json-render card
 *      (`host.chat.append`).
 *
 * Plus phone-portrait versions of 1 and 2 at the mobile spec's
 * viewport and scale.
 *
 * Also writes `extension-zones.json`: the bounding boxes of the
 * surfaces an extension contributes, as percentages of each image,
 * measured with `locator.boundingBox()` so the landing page can put
 * numbered markers on them.
 *
 * Run with:  npx playwright test e2e/landing-shot-extensions.spec.ts
 * Output:    LANDING_SHOT_DIR (defaults to ./landing-shots)
 */

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import type { Locator, Page } from "@playwright/test";

import { test, expect } from "./fixtures/embed";
import type { FixtureBackend } from "./fixtures/backend";
import { signedInStatus } from "./fixtures/backend";
import {
  CLAIMS,
  CLAIMS_ID,
  CLAIMS_NAME,
  DETAIL,
  claimsManifest,
  serveClaimsBundle,
} from "./fixtures/claims-desk";

const OUT = process.env.LANDING_SHOT_DIR || "landing-shots";
const ZONES_DIR = `${OUT}/.zones`;

const DESKTOP = { width: 1280, height: 720 };
const DESKTOP_SCALE = 1.5; // 1920x1080 PNG
const PHONE = { width: 460, height: 660 };
const PHONE_SCALE = 2;

// The shipped model. Not 27b.
const MODEL = "igni 1.0 4b";
const PROJECT = "/srv/claims";
const ORG = "Acme Insurance";
const EMAIL = "sanne.devries@acme-insurance.nl";

const SESSIONS = [
  { session_id: "c1a7e0f2", name: "Water damage, Keizersgracht" },
  { session_id: "c2b81d93", name: "Vehicle claim CL-20421, third party" },
  { session_id: "c3c4f1a8", name: "Weekly fraud review" },
  { session_id: "c4d09b71", name: "Storm damage, roof tiles Haarlem" },
  { session_id: "c5e6a2c0", name: "Letter to broker about excess" },
  { session_id: "c6f3b8d4", name: "Bicycle theft, missing receipt" },
];

type Theme = "light" | "dark";

/** Everything the client asks for at boot, answered as an insurer's
 *  on-prem install would. */
function stageBackend(be: FixtureBackend, opts: { app: boolean; origin?: string }) {
  const status = (ctx: number) =>
    signedInStatus({
      model: MODEL,
      context_tokens: ctx,
      max_context: 128_000,
      // On-prem: the organisation's own igni server, not a cloud. The
      // client draws no cloud symbol either way (`CloudIcon` in
      // `components/Icons.tsx` has no caller); see the report.
      cloud_connected: true,
      cloud_org: ORG,
      cloud_email: EMAIL,
    });
  be.onRpc("get_status", () => status(9_600));
  be.onRpc("get_session_id", () => SESSIONS[0].session_id);
  be.onRpc("get_project_dir", () => PROJECT);
  be.onRpc("count_context_tokens", () => 9_600);
  be.onRpc("get_login_state", () => ({ logged_in: true, email: EMAIL }));
  be.onRpc("list_sessions", () => ({ sessions: SESSIONS }));
  be.onRpc("get_group_policy", () => ({
    group_id: "grp-acme",
    group_name: ORG,
    fetched_at: "2026-10-30T08:00:00Z",
    entry_count: 12,
    default_model: MODEL,
  }));
  be.onRpc("upload_attachment", (args) => ({
    path: `${PROJECT}/CL-20418/${String(args.filename)}`,
    size: 184_000,
  }));

  if (!opts.app) {
    be.onRpc("list_extensions", () => ({ extensions: [] }));
    be.onRpc("notifications_counts", () => ({ unread: 0, by_source: {} }));
    return;
  }
  be.onRpc("list_extensions", () => ({ extensions: [claimsManifest(opts.origin!)] }));
  be.onRpc("notifications_counts", () => ({ unread: 3, by_source: { [CLAIMS_ID]: 3 } }));
  // The status chip's source tool, `contributes.indicators[].source`.
  be.onRpc(`ext.${CLAIMS_ID}.indicator`, () => ({
    label: "Claims: 4 open",
    tone: "warning",
    detail: "4 claims need a person",
  }));
  // The page's own call, over the bridge.
  be.onRpc(`ext.${CLAIMS_ID}.queue`, () => ({ claims: CLAIMS, detail: DETAIL }));
}

async function boot(page: Page, appUrl: string, theme: Theme) {
  await page.addInitScript((value) => window.localStorage.setItem("ember:theme", value), theme);
  await page.emulateMedia({ colorScheme: theme });
  await page.goto(appUrl);
  await expect(page.locator(".composer-editable")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
}

/** The id of the n-th user_message the client sent (1-based). */
async function runIdOf(be: FixtureBackend, n: number): Promise<string> {
  await expect
    .poll(() => be.received().filter((m) => m.type === "user_message").length)
    .toBeGreaterThanOrEqual(n);
  return String(be.received().filter((m) => m.type === "user_message")[n - 1].id);
}

function tool(
  be: FixtureBackend,
  runId: string,
  id: string,
  toolName: string,
  friendly: string,
  args: string,
  summary: string,
) {
  be.pushEvent({ type: "tool_started", id, run_id: runId, tool_name: toolName, friendly_name: friendly, args_summary: args });
  be.pushEvent({
    type: "tool_completed",
    id,
    run_id: runId,
    summary,
    full_result: summary,
    has_markup: false,
    diff_rows: null,
    is_error: false,
  });
}

function say(be: FixtureBackend, runId: string, text: string) {
  be.pushEvent({ type: "content_delta", id: runId, text });
}

function finish(be: FixtureBackend, runId: string) {
  be.pushEvent({ type: "stream_end", id: runId });
  be.pushEvent({ type: "run_completed", id: runId });
}

/** A tiny real JPEG-ish file for the attach flow. The fixture's
 *  `upload_attachment` never decodes it. */
const PHOTO = Buffer.from(
  "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=",
  "base64",
);

/** Ask about the Keizersgracht claim with the adjuster's photo. */
async function askAboutClaim(page: Page, be: FixtureBackend, opts: { short: boolean }) {
  const editor = page.locator(".composer-editable");
  await editor.click();
  await editor.type(
    opts.short
      ? "Check CL-20418 for me. Photo of the kitchen ceiling attached. "
      : "Can you check the file for CL-20418, the water damage at Keizersgracht? Photo of the kitchen ceiling attached. ",
  );
  await page
    .locator('input[type="file"]')
    .first()
    .setInputFiles({ name: "plafond-keuken.jpg", mimeType: "image/jpeg", buffer: PHOTO });
  await expect(editor).toContainText("plafond-keuken.jpg");
  await editor.press("Enter");

  const run = await runIdOf(be, 1);
  be.pushEvent({ type: "run_started", id: run });
  tool(be, run, "t1", "read_file", "Read", `${PROJECT}/CL-20418/schademelding.pdf`, "3 pages");
  if (!opts.short) tool(be, run, "t2", "read_file", "Read", `${PROJECT}/CL-20418/plafond-keuken.jpg`, "1 image");
  say(
    be,
    run,
    opts.short
      ? "A leak from the flat upstairs soaked the kitchen ceiling on 21 October. " +
          "The photo shows a stained, sagging ceiling of about 2 × 3 m. " +
          "This is covered; the €250 excess applies.\n\n" +
          "**Missing from the file:**\n\n" +
          "- the plumber's invoice\n" +
          "- a photo of the parquet\n" +
          "- the neighbour's insurer\n\n" +
          "Shall I ask Mrs Bakker for these?"
      : "**What happened.** A leak from the upstairs neighbour's bathroom on 21 October " +
          "soaked the kitchen ceiling. The photo shows a stained, sagging ceiling of about " +
          "2 × 3 m and water marks down the wall cupboards. No mould yet, which fits a recent leak.\n\n" +
          "**Cover.** Sudden water damage is covered under her Woonhuis Plus policy. " +
          "The €250 excess applies.\n\n" +
          "**Missing from the file:**\n\n" +
          "- the plumber's invoice for the repair upstairs\n" +
          "- a photo of the parquet the report mentions\n" +
          "- the neighbour's insurer and policy number, so we can recover the cost\n\n" +
          "The €6,850 claimed is in line with damage like this. " +
          "Shall I draft an email to Mrs Bakker asking for the three missing items?",
  );
  finish(be, run);
  await expect(page.locator(".msg-assistant").last()).toContainText("Mrs Bakker");
}

/** Settle the transcript for a still: at the bottom, no "jump to
 *  latest" chevron, no animations mid-flight. */
async function settle(page: Page) {
  // A focused composer draws its accent ring; nobody is typing in a still.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.addStyleTag({
    content: `.scroll-to-bottom { display: none !important; }
      *, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }`,
  });
  await page.waitForTimeout(900);
}

type Box = { x: number; y: number; width: number; height: number };

/** A locator's box as percentages of the captured image. */
async function zone(locator: Locator, view: { width: number; height: number }): Promise<Box | null> {
  if ((await locator.count()) === 0) return null;
  const box = await locator.first().boundingBox();
  if (!box) return null;
  // Present but covered — the status line sits under an open page — is
  // not in the image, so it gets no marker.
  const onTop = await locator.first().evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit !== null && (el === hit || el.contains(hit));
  });
  if (!onTop) return null;
  // Clip to the image: the composer's shadow or a frame taller than the
  // fold would otherwise read as a marker off the edge.
  const x0 = Math.max(0, box.x);
  const y0 = Math.max(0, box.y);
  const x1 = Math.min(view.width, box.x + box.width);
  const y1 = Math.min(view.height, box.y + box.height);
  if (x1 <= x0 || y1 <= y0) return null;
  const pct = (v: number, of: number) => Math.round((v / of) * 10_000) / 100;
  return {
    x: pct(x0, view.width),
    y: pct(y0, view.height),
    width: pct(x1 - x0, view.width),
    height: pct(y1 - y0, view.height),
  };
}

/** One shot's zones. Each test writes its own fragment and re-merges
 *  them all, so the parallel runner cannot lose one. */
function recordZones(shot: string, image: string, zones: Record<string, Box | null>, notes: string[] = []) {
  mkdirSync(ZONES_DIR, { recursive: true });
  writeFileSync(`${ZONES_DIR}/${shot}.json`, JSON.stringify({ image, zones, notes }, null, 2));
  const merged: Record<string, unknown> = {
    units: "percent of the image (x, y = top-left corner); the same for the light and dark PNG of each shot",
    measured_with: "locator.boundingBox() in e2e/landing-shot-extensions.spec.ts",
  };
  for (const file of readdirSync(ZONES_DIR).sort()) {
    if (!file.endsWith(".json")) continue;
    merged[file.replace(/\.json$/, "")] = JSON.parse(readFileSync(`${ZONES_DIR}/${file}`, "utf8"));
  }
  writeFileSync(`${OUT}/extension-zones.json`, JSON.stringify(merged, null, 2) + "\n");
}

/** The surfaces the Claims desk contributes, located in the client. */
function surfaces(page: Page) {
  return {
    sidebar_app_entry: page.locator(".sidebar-link", { hasText: CLAIMS_NAME }),
    extension_page_frame: page.locator("iframe.extension-frame"),
    indicator_chip: page.locator(".statusline button", { hasText: "Claims: 4 open" }),
    json_render_card: page.locator(".jr-view"),
    extension_message: page.locator(".msg-user", { has: page.locator(".msg-user-from") }),
    composer: page.locator(".composer"),
  };
}

async function openClaimsPage(page: Page) {
  const entry = page.locator(".sidebar-link", { hasText: CLAIMS_NAME });
  if ((page.viewportSize()?.width ?? 0) > 700) {
    await entry.click();
  } else {
    // Phone: the sidebar is folded away, so open the page the other
    // real way — its status chip, whose manifest names the page.
    await page.locator(".statusline button", { hasText: "Claims: 4 open" }).click();
  }
  const frame = page.frameLocator("iframe.extension-frame");
  await expect(frame.locator("body.ready")).toBeVisible({ timeout: 10_000 });
  await expect(frame.locator("tbody tr")).toHaveCount(CLAIMS.length);
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`desktop ${theme}`, () => {
    test.use({ viewport: DESKTOP, deviceScaleFactor: DESKTOP_SCALE });

    test(`1 plain igni, ${theme}`, async ({ page, backend, appUrl }) => {
      stageBackend(backend, { app: false });
      await boot(page, appUrl, theme);
      await askAboutClaim(page, backend, { short: false });
      // The question and its photo at the top, under the header.
      await page.evaluate(() => {
        const bubble = document.querySelector(".msg-user");
        const scroller = document.querySelector(".conversation");
        if (!bubble || !scroller) return;
        scroller.scrollTop +=
          bubble.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 72;
      });
      await settle(page);
      // No cloud symbol anywhere: `CloudIcon`'s path is nowhere on the page.
      await expect(page.locator('path[d^="M4.5 12.5a3 3 0 0 1-.4-5.97"]')).toHaveCount(0);
      await page.screenshot({ path: `${OUT}/igni-claims-plain-${theme}.png`, clip: { x: 0, y: 0, ...DESKTOP } });
    });

    test(`2 claims desk page, ${theme}`, async ({ page, backend, appUrl }) => {
      const bundle = await serveClaimsBundle();
      try {
        stageBackend(backend, { app: true, origin: bundle.origin });
        await boot(page, appUrl, theme);
        const s = surfaces(page);
        await expect(s.sidebar_app_entry).toBeVisible();
        await expect(s.sidebar_app_entry.locator(".extension-badge")).toHaveText("3");
        await expect(s.indicator_chip).toBeVisible();
        await openClaimsPage(page);

        // The tools menu is not in this shot: it opens from the
        // composer's "/" button, and the composer is hidden while a page
        // is open (`.main--paged > .composer-zone` in theme.css).
        await expect(s.composer).toBeHidden();
        await settle(page);

        await page.screenshot({ path: `${OUT}/igni-claims-app-${theme}.png`, clip: { x: 0, y: 0, ...DESKTOP } });
        recordZones("app", "igni-claims-app-{light,dark}.png", {
          sidebar_app_entry: await zone(s.sidebar_app_entry, DESKTOP),
          extension_page_frame: await zone(s.extension_page_frame, DESKTOP),
          indicator_chip: await zone(s.indicator_chip, DESKTOP),
          menu_entry: null,
          composer: null,
        }, [
          "indicator_chip: null when the open page covers the status line it sits in (it does in this client).",
          "menu_entry: not captured. The tools menu opens from the composer, which is hidden while an extension page is open.",
          "composer: hidden while a page is open, so not in this image.",
        ]);
      } finally {
        await bundle.close();
      }
    });

    test(`3 chat with the app, ${theme}`, async ({ page, backend, appUrl }) => {
      const bundle = await serveClaimsBundle();
      try {
        stageBackend(backend, { app: true, origin: bundle.origin });
        await boot(page, appUrl, theme);
        const s = surfaces(page);
        await expect(s.indicator_chip).toBeVisible();

        const editor = page.locator(".composer-editable");
        await editor.click();
        await editor.type("What's waiting for me in the claims queue?");
        await editor.press("Enter");

        const run1 = await runIdOf(backend, 1);
        backend.pushEvent({ type: "run_started", id: run1 });
        tool(backend, run1, "q1", "claims_list_queue", "claims_list_queue", "status=needs_person", "4 claims");
        say(
          backend,
          run1,
          "Four claims need a person. Two are yours: **CL-20421**, the third-party vehicle claim, " +
            "is waiting on the other driver's statement, and **CL-20433**, the kitchen fire, " +
            "needs a loss adjuster booked. The other two sit with Femke and Daan.",
        );
        finish(backend, run1);
        await expect(page.locator(".msg-assistant").last()).toContainText("Femke and Daan");

        // The Claims desk posts as itself (`host.chat.post`): a new
        // claim came in from the broker portal.
        backend.pushEvent({
          type: "user_message_received",
          text:
            "New claim CL-20431 from the broker portal: water damage, €2,400. " +
            "It is over the €2,000 limit for a single reviewer.",
          client_id: "ext-acme-claims",
          queued: false,
          extension_id: CLAIMS_ID,
        });
        await expect(s.extension_message).toBeVisible();

        const run2 = "run-ext-2";
        backend.pushEvent({ type: "run_started", id: run2 });
        tool(backend, run2, "q2", "claims_get_claim", "claims_get_claim", "CL-20431", "1 claim · 4 documents");
        // And appends a card (`host.chat.append`) — the backend's push
        // for it, exactly as `HostChat.show` builds it.
        const spec = {
          root: "card",
          elements: {
            card: {
              type: "Card",
              props: { title: "CL-20431 · Water damage", subtitle: "Prinsengracht 274-H, Amsterdam · reported Monday" },
              children: ["grid", "foot"],
            },
            grid: { type: "Grid", props: { columns: 3, gap: "md" }, children: ["amount", "docs", "limit"] },
            amount: { type: "Metric", props: { label: "Claimed", value: "2,400", prefix: "€" } },
            docs: { type: "Metric", props: { label: "Documents", value: "4 of 4" } },
            limit: { type: "Metric", props: { label: "Single-reviewer limit", value: "2,000", prefix: "€" } },
            foot: { type: "Grid", props: { columns: 3, gap: "md" }, children: ["badge", "who", "assign"] },
            badge: { type: "Badge", props: { text: "Needs a second reviewer", tone: "warning" } },
            who: { type: "Text", props: { text: "Suggested: Daan Visser", muted: true } },
            assign: {
              type: "Button",
              props: { text: "Assign", variant: "primary" },
              on: { press: { action: "assign", params: { claim: "CL-20431" } } },
            },
          },
        };
        backend.pushEvent({
          type: "push_notification",
          channel: "orchestrate_event",
          payload: {
            type: "visualization_delta",
            agent_path: `ext:${CLAIMS_ID}`,
            spec_id: "ext-3f9a1c20b7e4",
            json: JSON.stringify(spec),
            final: true,
            title: "New claim",
            source: CLAIMS_ID,
          },
        });
        say(
          backend,
          run2,
          "The file is complete: report, four photos and the plumber's quote. Only the second " +
            "review is missing. Daan has the fewest open claims; shall I assign it to him?",
        );
        finish(backend, run2);
        await expect(s.json_render_card).toBeVisible();
        await expect(page.locator(".msg-assistant").last()).toContainText("assign it to him");
        // The card is the app's: its title, and the app's name.
        await expect(page.locator(".jr-view-title")).toHaveText("New claim");
        await expect(page.locator(".jr-view-source")).toHaveText(`via ${CLAIMS_NAME}`);

        await page.mouse.move(640, 360);
        await page.mouse.wheel(0, 4000);
        await settle(page);

        await page.screenshot({ path: `${OUT}/igni-claims-chat-${theme}.png`, clip: { x: 0, y: 0, ...DESKTOP } });
        recordZones("chat", "igni-claims-chat-{light,dark}.png", {
          sidebar_app_entry: await zone(s.sidebar_app_entry, DESKTOP),
          indicator_chip: await zone(s.indicator_chip, DESKTOP),
          extension_message: await zone(s.extension_message, DESKTOP),
          json_render_card: await zone(s.json_render_card, DESKTOP),
          composer: await zone(s.composer, DESKTOP),
        });
      } finally {
        await bundle.close();
      }
    });
  });

  test.describe(`phone ${theme}`, () => {
    test.use({ viewport: PHONE, deviceScaleFactor: PHONE_SCALE });

    test(`1 plain igni, phone, ${theme}`, async ({ page, backend, appUrl }) => {
      stageBackend(backend, { app: false });
      await boot(page, appUrl, theme);
      await askAboutClaim(page, backend, { short: true });
      await settle(page);
      await page.screenshot({ path: `${OUT}/igni-claims-plain-mobile-${theme}.png`, clip: { x: 0, y: 0, ...PHONE } });
    });

    test(`2 claims desk page, phone, ${theme}`, async ({ page, backend, appUrl }) => {
      const bundle = await serveClaimsBundle();
      try {
        stageBackend(backend, { app: true, origin: bundle.origin });
        await boot(page, appUrl, theme);
        const s = surfaces(page);
        await expect(s.indicator_chip).toBeVisible();
        await openClaimsPage(page);
        await settle(page);
        await page.screenshot({ path: `${OUT}/igni-claims-app-mobile-${theme}.png`, clip: { x: 0, y: 0, ...PHONE } });
        recordZones(
          "app_mobile",
          "igni-claims-app-mobile-{light,dark}.png",
          {
            sidebar_app_entry: await zone(s.sidebar_app_entry, PHONE),
            extension_page_frame: await zone(s.extension_page_frame, PHONE),
            indicator_chip: await zone(s.indicator_chip, PHONE),
          },
          [
            "sidebar_app_entry: the sidebar is folded away at phone width, so it is not in the image. The page was opened from the indicator chip.",
            "indicator_chip: null when the open page covers the status line it sits in (it does in this client).",
          ],
        );
      } finally {
        await bundle.close();
      }
    });
  });
}
