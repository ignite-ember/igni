/**
 * Real-BE smoke test: spawn the actual Python BE process, drive the
 * FE against it, exercise one round-trip. Catches wire-format drift
 * between the Python emitter and the TypeScript decoder that the
 * JS-fixture suite cannot see (their schemas evolve independently).
 *
 * Not run by default in ``npm test`` workflows where Python isn't on
 * PATH; gated by ``EMBER_E2E_REAL_BE=1``. CI sets that var on hosts
 * with the venv prepared.
 *
 * What's covered:
 *   - The BE prints its ``{"status":"ready","ws_port":N}`` envelope
 *     on stdout and binds an actual loopback WS server.
 *   - The FE's ``protocol/client.ts`` decoder accepts the Welcome and
 *     every RPC reply the boot flow produces (get_session_id,
 *     get_status, get_client_state, …) without throwing on an
 *     unknown field or a missing default.
 *   - The composer reaches the "ready" placeholder, which is the
 *     same end-state real users see post-connect.
 *
 * What's NOT covered (deliberately): the agent loop. Driving an
 * actual ``run_message`` would call the LLM — out of scope for a
 * wire-format smoke. The cancel/HITL/long-stream paths are tested
 * separately in the Python-side integration suite.
 */

import { test as base, expect } from "@playwright/test";
import {
  ChildProcessWithoutNullStreams,
  spawn,
} from "node:child_process";
import * as path from "node:path";
import * as os from "node:os";
import * as fs from "node:fs/promises";
import { fileURLToPath } from "node:url";

type Fixtures = {
  realBe: { wsUrl: string; projectDir: string };
};

// Project root: walk up from clients/web/e2e to the repo root.
// Playwright runs specs as ES modules — ``__dirname`` isn't defined,
// derive it from ``import.meta.url``.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..", "..");
const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");

const test = base.extend<Fixtures>({
  realBe: async ({}, use) => {
    if (process.env.EMBER_E2E_REAL_BE !== "1") {
      test.skip(
        true,
        "Set EMBER_E2E_REAL_BE=1 to run; requires the project venv.",
      );
    }

    const projectDir = await fs.mkdtemp(
      path.join(os.tmpdir(), "ember-real-be-"),
    );

    // Copy the repo's local config into the fixture's tmpdir so the
    // BE's registry lookup finds the project's default model
    // (``MiniMax-M2.7``). Without this, the BE's default-agent
    // build hits ``ValueError: Unknown model 'MiniMax-M2.7'`` and
    // never reaches the ready line. Best-effort: a missing source
    // file (e.g. CI without local config) leaves the BE in its
    // pre-fix state — same failure, just earlier in the stack.
    const localConfig = path.join(REPO_ROOT, ".ember", "config.local.yaml");
    try {
      await fs.mkdir(path.join(projectDir, ".ember"), { recursive: true });
      await fs.copyFile(localConfig, path.join(projectDir, ".ember", "config.local.yaml"));
    } catch (err) {
      console.warn(`realBe fixture: could not copy ${localConfig}: ${err}`);
    }

    const proc: ChildProcessWithoutNullStreams = spawn(
      VENV_PYTHON,
      [
        "-m",
        "ember_code.backend",
        "--ws-port",
        "0",
        "--project-dir",
        projectDir,
      ],
      {
        cwd: REPO_ROOT,
        env: {
          ...process.env,
          // The watchdog (``_watch_parent``) self-terminates the BE
          // when our PID disappears — guarantees no orphaned BEs even
          // if Playwright crashes mid-test.
          EMBER_PARENT_PID: String(process.pid),
        },
      },
    );

    // Capture stderr for diagnostics if startup fails.
    let stderrBuf = "";
    proc.stderr.on("data", (chunk) => {
      stderrBuf += chunk.toString();
    });

    const wsUrl = await new Promise<string>((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(
          new Error(
            `BE did not emit ready line within 30s.\nstderr:\n${stderrBuf}`,
          ),
        );
      }, 30_000);
      let stdoutBuf = "";
      proc.stdout.on("data", (chunk) => {
        stdoutBuf += chunk.toString();
        // Ready line is its own JSON envelope on its own line.
        for (const line of stdoutBuf.split("\n")) {
          const t = line.trim();
          if (!t.startsWith("{")) continue;
          try {
            const parsed = JSON.parse(t);
            if (parsed.status === "ready" && parsed.ws_url) {
              clearTimeout(timeout);
              resolve(String(parsed.ws_url));
              return;
            }
          } catch {
            // partial JSON line; keep buffering
          }
        }
      });
      proc.on("exit", (code) => {
        clearTimeout(timeout);
        reject(
          new Error(
            `BE exited with code ${code} before ready.\nstderr:\n${stderrBuf}`,
          ),
        );
      });
    });

    try {
      await use({ wsUrl, projectDir });
    } finally {
      // Shutdown: SIGTERM first; if it lingers > 3s, hard kill. The
      // Agno team's tail-drain can take a beat after ``shutdown()``.
      proc.kill("SIGTERM");
      await new Promise<void>((resolve) => {
        const t = setTimeout(() => {
          try {
            proc.kill("SIGKILL");
          } catch {
            /* already dead */
          }
          resolve();
        }, 3_000);
        proc.on("exit", () => {
          clearTimeout(t);
          resolve();
        });
      });
      await fs.rm(projectDir, { recursive: true, force: true });
    }
  },
});

test.describe("real BE wire format", () => {
  test("boot → connected: FE decodes every RPC the real BE returns", async ({
    page,
    realBe,
  }) => {
    // FE talks to the real loopback BE via ``?ws=`` query param.
    await page.goto(`/?ws=${encodeURIComponent(realBe.wsUrl)}`);

    // The composer's placeholder is set by the connection state
    // machine — flipping from "Connecting…" to "Message Ember"
    // means: WS open, Welcome consumed, every boot RPC's reply
    // decoded without throwing. If the BE emitted any envelope the
    // FE schema can't parse, the connection-ready transition
    // wouldn't fire and this would timeout.
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
      { timeout: 30_000 },
    );

    // The model chip shows whatever the real BE put in status_update
    // — we only assert it's *something* (the registry depends on
    // local config) and not the "—" placeholder a missing reply
    // would leave. Catches a regression where status_update arrives
    // but the FE drops the model field due to a schema mismatch.
    const modelText = await page
      .locator(".composer-model")
      .first()
      .textContent({ timeout: 10_000 });
    expect(modelText).toBeTruthy();
    expect(modelText?.trim()).not.toBe("");
    expect(modelText?.trim()).not.toBe("—");
  });
});
