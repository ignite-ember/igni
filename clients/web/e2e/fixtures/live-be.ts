/**
 * A live Python backend for the e2e suite.
 *
 * This used to live inside ``real-be.spec.ts`` behind
 * ``IGNI_E2E_REAL_BE=1``, and the specs that most needed it —
 * ``live-be-plan-decision``, which is the only test that proves the
 * BE's RPC dispatch table routes ``approve_plan`` to the right method
 * with the right argument — sat behind a *different* var,
 * ``IGNI_LIVE_WS``, that nothing set. They ran nowhere. See F128.
 *
 * The gate is now a capability rather than a flag, in this order:
 *
 *   1. ``IGNI_LIVE_WS`` — a backend the developer already has running.
 *      Fastest loop, and the only way to point a spec at the build
 *      you are staring at.
 *   2. Otherwise spawn one, if the project venv exists.
 *   3. Otherwise skip, saying which of the two is missing.
 *
 * A forgotten environment variable is invisible; a missing venv is a
 * fact about the machine. The difference matters, because the first
 * kind of skip is a mistake and the second is a limitation, and only
 * the second should be allowed to pass quietly — which is what
 * ``reporters/skips.ts`` enforces.
 */

import { test as base } from "@playwright/test";
import {
  ChildProcessWithoutNullStreams,
  spawn,
} from "node:child_process";
import * as path from "node:path";
import * as os from "node:os";
import * as fs from "node:fs";
import * as fsp from "node:fs/promises";
import { fileURLToPath } from "node:url";

export type LiveBe = {
  /** ``ws://127.0.0.1:PORT`` — what the FE takes as ``?ws=``. */
  wsUrl: string;
  /**
   * The project directory the backend was started against, or null
   * when we are borrowing a backend from ``IGNI_LIVE_WS`` and have no
   * idea where its project lives.
   */
  projectDir: string | null;
  /** How this backend came to be — asserted by the fixture's own test. */
  origin: "spawned" | "IGNI_LIVE_WS";
};

// Project root: walk up from clients/web/e2e/fixtures to the repo root.
// Playwright runs specs as ES modules, so ``__dirname`` is undefined.
const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..", "..", "..");
export const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");

/** The reason a run cannot have a backend, or null if it can. */
export function whyNoBackend(env: NodeJS.ProcessEnv = process.env): string | null {
  if (env.IGNI_LIVE_WS) return null;
  if (fs.existsSync(VENV_PYTHON)) return null;
  return (
    `no backend available: ${VENV_PYTHON} does not exist and ` +
    `IGNI_LIVE_WS is unset. Create the venv (\`uv sync\` at the repo ` +
    `root) or point IGNI_LIVE_WS at a running backend.`
  );
}

/**
 * The backend refuses to build its default agent unless the configured
 * model resolves against ``models.registry``, and that registry ships
 * empty — entries arrive from a cloud login or from local config. So
 * write one.
 *
 * It must not read the developer's ``.igni/config.local.yaml``:
 * that path is gitignored, so it is whatever a given machine happens
 * to hold, and on the machine where this was first tried it named a
 * model the registry lacked — the backend died before printing its
 * ready line. Pointing ``models.default`` at a stub we wrote ourselves
 * makes this independent of both the developer's config and of
 * whatever the built-in default is currently called.
 *
 * The URL is never dialled. None of these specs drive the agent loop.
 */
const STUB_MODEL = "e2e-wire-format-stub";

async function writeStubConfig(projectDir: string): Promise<void> {
  await fsp.mkdir(path.join(projectDir, ".igni"), { recursive: true });
  await fsp.writeFile(
    path.join(projectDir, ".igni", "config.local.yaml"),
    [
      "models:",
      `  default: ${STUB_MODEL}`,
      "  registry:",
      `    ${STUB_MODEL}:`,
      "      provider: openai_like",
      `      model_id: ${STUB_MODEL}`,
      "      url: http://127.0.0.1:9/v1",
      "      context_window: 8192",
      "      vision: false",
      "",
    ].join("\n"),
    "utf8",
  );
}

function readyUrl(proc: ChildProcessWithoutNullStreams): Promise<string> {
  let stderrBuf = "";
  proc.stderr.on("data", (chunk) => {
    stderrBuf += chunk.toString();
  });
  return new Promise<string>((resolve, reject) => {
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
      // The ready line is its own JSON envelope on its own line.
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
}

async function stop(proc: ChildProcessWithoutNullStreams): Promise<void> {
  // SIGTERM first; hard kill if it lingers past 3s. The Agno team's
  // tail-drain can take a beat after ``shutdown()``.
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
}

export const test = base.extend<{ liveBe: LiveBe; liveWsUrl: string }>({
  liveBe: async ({}, use) => {
    const borrowed = process.env.IGNI_LIVE_WS;
    if (borrowed) {
      await use({ wsUrl: borrowed, projectDir: null, origin: "IGNI_LIVE_WS" });
      return;
    }

    const why = whyNoBackend();
    if (why) test.skip(true, why);

    const projectDir = await fsp.mkdtemp(
      path.join(os.tmpdir(), "igni-live-be-"),
    );
    await writeStubConfig(projectDir);

    const proc: ChildProcessWithoutNullStreams = spawn(
      VENV_PYTHON,
      ["-m", "ember_code.backend", "--ws-port", "0", "--project-dir", projectDir],
      {
        cwd: REPO_ROOT,
        env: {
          ...process.env,
          // ``_watch_parent`` self-terminates the backend when our PID
          // disappears — no orphaned backends even if Playwright dies
          // mid-test.
          IGNI_PARENT_PID: String(process.pid),
        },
      },
    );

    try {
      const wsUrl = await readyUrl(proc);
      await use({ wsUrl, projectDir, origin: "spawned" });
    } finally {
      await stop(proc);
      await fsp.rm(projectDir, { recursive: true, force: true });
    }
  },

  liveWsUrl: async ({ liveBe }, use) => {
    await use(liveBe.wsUrl);
  },
});

export { expect } from "@playwright/test";
