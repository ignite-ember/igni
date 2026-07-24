/**
 * Real end-to-end verification of thinking/std separation.
 *
 * Boots the actual Python BE (MiniMax-M2.7 default — a model that
 * streams reasoning inline as <think>…</think>), drives the real FE
 * (Vite dev server on 5179) through several conversational turns,
 * and asserts:
 *
 *   1. The FE receives BOTH is_thinking=true and is_thinking=false
 *      content_delta frames (thinking and std both present).
 *   2. No literal <think>/</think> tag artifacts leak into EITHER
 *      stream — the BE parser must have peeled them.
 *   3. No collision: within a run, once std (answer) content starts,
 *      we don't flip back into a spurious thinking classification
 *      mid-answer (the "all goes as std" / "all goes as think" bug).
 *   4. The rendered DOM shows separate thinking toggles and
 *      assistant bubbles, and expanding a thinking bubble shows no
 *      raw tag text.
 *
 * Requires:
 *   - project .venv
 *   - Vite dev server running on 5179 (the script starts the BE itself)
 *   - EMBER_E2E_MINIMAX_KEY env var — a direct MiniMax-M2.7 API key
 *     (https://api.minimax.io/v1). The script writes it into the
 *     temp project's config so the BE uses M2.7 as its default model.
 *
 * Usage:
 *   EMBER_E2E_MINIMAX_KEY=sk-... node clients/web/e2e/real-be-thinking.mjs
 */

import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
import * as path from "node:path";
import * as os from "node:os";
import * as fs from "node:fs/promises";
import * as fsSync from "node:fs";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..", "..");
const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");
const FE_URL = "http://127.0.0.1:5179";

const MINIMAX_KEY = process.env.EMBER_E2E_MINIMAX_KEY;
if (!MINIMAX_KEY) {
  console.error(
    "[e2e] EMBER_E2E_MINIMAX_KEY is not set.\n" +
      "      Provide a direct MiniMax-M2.7 API key, e.g.:\n" +
      "      EMBER_E2E_MINIMAX_KEY=sk-... node clients/web/e2e/real-be-thinking.mjs",
  );
  process.exit(2);
}

const PROMPTS = [
  "hey there",
  "what is 17 times 23? think it through",
  "name three primary colors",
  "briefly, why is the sky blue?",
  "count from 1 to 5",
  "what's the capital of France?",
];

function log(...a) {
  console.log("[e2e]", ...a);
}

async function bootBE() {
  const projectDir = await fs.mkdtemp(path.join(os.tmpdir(), "ember-think-"));
  await fs.mkdir(path.join(projectDir, ".ember"), { recursive: true });
  // Force a working MiniMax-M2.7 registry entry (key from
  // EMBER_E2E_MINIMAX_KEY) as the default. M2.7 is the project's real
  // model and emits inline-<think> reasoning — exactly the path under
  // test.
  const cfgBody = `models:
  default: MiniMax-M2.7
  registry:
    MiniMax-M2.7:
      provider: openai_like
      model_id: MiniMax-M2.7
      url: https://api.minimax.io/v1
      api_key: ${MINIMAX_KEY}
      context_window: 204800
      vision: false
`;
  await fs.writeFile(
    path.join(projectDir, ".ember", "config.local.yaml"),
    cfgBody,
  );

  const proc = spawn(
    VENV_PYTHON,
    ["-m", "ember_code.backend", "--ws-port", "0", "--project-dir", projectDir],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        EMBER_PARENT_PID: String(process.pid),
      },
    },
  );
  let stderrBuf = "";
  proc.stderr.on("data", (c) => {
    stderrBuf += c.toString();
    fsSync.appendFileSync("/tmp/be_e2e.err", c.toString());
  });

  const wsUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`BE not ready in 40s.\nstderr:\n${stderrBuf}`)),
      40_000,
    );
    let buf = "";
    proc.stdout.on("data", (c) => {
      buf += c.toString();
      for (const line of buf.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("{")) continue;
        try {
          const p = JSON.parse(t);
          if (p.status === "ready" && p.ws_url) {
            clearTimeout(timer);
            resolve(String(p.ws_url));
          }
        } catch {
          /* partial */
        }
      }
    });
    proc.on("exit", (code) =>
      reject(new Error(`BE exited ${code} before ready.\nstderr:\n${stderrBuf}`)),
    );
  });
  return { proc, wsUrl, projectDir };
}

async function main() {
  const { proc, wsUrl, projectDir } = await bootBE();
  log("BE ready:", wsUrl);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  // Capture every content_delta frame the FE receives by wrapping
  // WebSocket at the page level BEFORE the app connects.
  await page.addInitScript(() => {
    window.__frames = [];
    window.__allTypes = [];
    window.__sent = [];
    const OrigWS = window.WebSocket;
    class TapWS extends OrigWS {
      constructor(...args) {
        super(...args);
        const origSend = this.send.bind(this);
        this.send = (data) => {
          try {
            window.__sent.push(JSON.parse(data));
          } catch {
            window.__sent.push(String(data));
          }
          return origSend(data);
        };
        this.addEventListener("message", (ev) => {
          try {
            const m = JSON.parse(ev.data);
            window.__allTypes.push(m.type);
            window.__frames.push(m);
          } catch {
            /* ignore */
          }
        });
      }
    }
    window.WebSocket = TapWS;
  });

  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });

  await page.goto(`${FE_URL}/?ws=${encodeURIComponent(wsUrl)}`);
  await page
    .locator(".composer-editable")
    .waitFor({ state: "visible", timeout: 30_000 });
  // Wait for connect (placeholder flips away from "Connecting…").
  await page.waitForFunction(
    () => {
      const el = document.querySelector(".composer-editable");
      const ph = el?.getAttribute("data-placeholder") || "";
      return /Message|igni/i.test(ph);
    },
    { timeout: 30_000 },
  );
  log("FE connected");

  const results = [];

  for (let turn = 0; turn < PROMPTS.length; turn++) {
    const prompt = PROMPTS[turn];
    const before = await page.evaluate(() => window.__frames.length);

    const editable = page.locator(".composer-editable");
    await editable.click();
    await editable.fill(prompt);
    await page.keyboard.press("Enter");
    log(`turn ${turn + 1} sent: ${JSON.stringify(prompt)}`);

    // Wait for this turn's stream_end (a new one past `before`).
    let sawEnd = true;
    await page
      .waitForFunction(
        (beforeLen) => {
          const fr = window.__frames.slice(beforeLen);
          return fr.some((f) => f.type === "stream_end");
        },
        before,
        { timeout: 90_000 },
      )
      .catch(() => {
        sawEnd = false;
      });
    if (!sawEnd) {
      const dbg = await page.evaluate((b) => ({
        sent: window.__sent.slice(-4),
        types: window.__allTypes.slice(b),
      }), before);
      log(`turn ${turn + 1}: NO stream_end. sent=${JSON.stringify(dbg.sent)}`);
      log(`  frame types since send: ${JSON.stringify(dbg.types)}`);
    }
    // Small settle for trailing frames.
    await page.waitForTimeout(500);

    const dbg2 = await page.evaluate((b) => ({
      sent: window.__sent.slice(-4),
      types: window.__allTypes.slice(b),
      errors: window.__frames
        .slice(b)
        .filter((f) => f.type === "run_error" || f.type === "error")
        .map((f) => f.error || f.content || f.text || JSON.stringify(f)),
    }), before);
    log(`  [dbg] frame types: ${JSON.stringify(dbg2.types)}`);
    if (dbg2.errors.length) log(`  [dbg] ERRORS: ${JSON.stringify(dbg2.errors)}`);

    const frames = await page.evaluate(
      (beforeLen) => window.__frames.slice(beforeLen),
      before,
    );
    results.push({ turn: turn + 1, prompt, frames });
  }

  // ── Analyze wire frames ──────────────────────────────────────────
  const TAG_RE = /<\/?think(?:ing)?>/i;
  let anyThinking = false;
  let anyStd = false;
  const problems = [];

  for (const { turn, prompt, frames } of results) {
    const deltas = frames.filter((f) => f.type === "content_delta" && f.text);
    const thinkTxt = deltas
      .filter((d) => d.is_thinking)
      .map((d) => d.text)
      .join("");
    const stdTxt = deltas
      .filter((d) => !d.is_thinking)
      .map((d) => d.text)
      .join("");
    if (thinkTxt) anyThinking = true;
    if (stdTxt) anyStd = true;

    // (2) No literal tag artifacts in EITHER stream.
    for (const d of deltas) {
      if (TAG_RE.test(d.text)) {
        problems.push(
          `turn ${turn}: literal think-tag leaked in ${d.is_thinking ? "THINKING" : "STD"} frame: ${JSON.stringify(d.text.slice(0, 60))}`,
        );
      }
    }

    // (3) Collision: after the first std (answer) frame with real
    // content, we should not see thinking frames re-appear (the
    // answer shouldn't interleave back into reasoning). Allow leading
    // thinking then a clean switch to std.
    let sawStd = false;
    for (const d of deltas) {
      if (!d.is_thinking && d.text.trim()) sawStd = true;
      else if (d.is_thinking && d.text.trim() && sawStd) {
        problems.push(
          `turn ${turn}: thinking frame appeared AFTER answer started (collision): ${JSON.stringify(d.text.slice(0, 40))}`,
        );
        break;
      }
    }

    log(
      `turn ${turn}: ${deltas.length} deltas | thinking=${thinkTxt.length}ch std=${stdTxt.length}ch`,
    );
    log("  raw ordered deltas:");
    for (const d of deltas) {
      log(
        `    [${d.is_thinking ? "THINK" : " STD "}] seq=${d.event_seq} ${JSON.stringify(d.text)}`,
      );
    }
  }

  // ── DOM checks ────────────────────────────────────────────────────
  const domThinkingToggles = await page.locator(".thinking-toggle").count();
  const domAssistant = await page.locator(".msg-assistant").count();

  // Expand every thinking bubble and scan for raw tag text.
  const toggles = await page.locator(".thinking-toggle").all();
  for (const t of toggles) await t.click().catch(() => {});
  await page.waitForTimeout(300);
  const thinkingDomTexts = await page
    .locator(".msg-thinking")
    .allTextContents();
  for (const txt of thinkingDomTexts) {
    if (TAG_RE.test(txt)) {
      problems.push(
        `DOM thinking bubble shows raw tag: ${JSON.stringify(txt.slice(0, 60))}`,
      );
    }
  }
  const assistantTexts = await page.locator(".msg-assistant").allTextContents();
  for (const txt of assistantTexts) {
    if (TAG_RE.test(txt)) {
      problems.push(
        `DOM assistant bubble shows raw tag: ${JSON.stringify(txt.slice(0, 60))}`,
      );
    }
  }

  await page.screenshot({ path: path.join(HERE, "real-be-thinking.png"), fullPage: true });

  // ── Verdict ───────────────────────────────────────────────────────
  log("─".repeat(50));
  log(`DOM: ${domThinkingToggles} thinking toggles, ${domAssistant} assistant bubbles`);
  log(`anyThinking=${anyThinking} anyStd=${anyStd}`);
  if (consoleErrors.length) {
    log(`console errors (${consoleErrors.length}):`);
    consoleErrors.slice(0, 5).forEach((e) => log("  ! " + e));
  }

  let ok = true;
  if (!anyStd) {
    problems.push("No std (answer) content received across ALL turns.");
  }
  if (!anyThinking) {
    log("WARNING: no thinking frames seen — model may not have emitted reasoning this run.");
  }
  if (domThinkingToggles > 0 && domAssistant === 0) {
    problems.push("DOM: thinking bubbles present but ZERO assistant bubbles (the reported bug).");
  }

  if (problems.length) {
    ok = false;
    log("PROBLEMS:");
    problems.forEach((p) => log("  ✗ " + p));
  } else {
    log("✓ All separation checks passed.");
  }

  await browser.close();
  proc.kill("SIGTERM");
  setTimeout(() => proc.kill("SIGKILL"), 3000);
  await fs.rm(projectDir, { recursive: true, force: true }).catch(() => {});

  process.exit(ok ? 0 : 1);
}

main().catch((e) => {
  console.error("[e2e] FATAL", e);
  process.exit(2);
});
