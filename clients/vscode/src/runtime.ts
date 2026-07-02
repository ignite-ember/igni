/**
 * Managed-runtime bootstrap for the Ember backend.
 *
 * Mirrors ``EmberRuntime.kt`` from the JetBrains plugin: on first
 * launch we download the ``uv`` binary for the current OS/arch, use
 * it to provision a pinned Python and install ``ignite-ember`` into
 * a managed venv, and return the venv's Python path. Subsequent
 * launches reuse the cache directly (sub-100ms overhead).
 *
 * Cache layout under the extension's globalStorage:
 *
 *   <globalStorage>/
 *     uv (or uv.exe on Windows)        ← downloaded once
 *     venv/                            ← per-plugin-version
 *       bin/python | Scripts/python.exe
 *     ember-install.json               ← marker recording installed versions
 *
 * **Dev override.** Setting ``EMBER_DEV_BACKEND=/abs/path/to/python``
 * bypasses the bootstrap and returns that path verbatim. The
 * ``emberCode.pythonPath`` user setting is honored the same way —
 * for users who want to point at their own venv (e.g. ember-code
 * contributors running an editable install). Both paths skip every
 * download + install step.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { IGNITE_EMBER_VERSION } from "./version.generated";

const PYTHON_VERSION = "3.12";
const UV_VERSION = "0.5.7";
const INSTALL_MARKER = "ember-install.json";

/**
 * Conservative free-space requirement before bootstrap starts.
 * Sized for uv + CPython + ignite-ember + transitives + the
 * sentence-transformer model + headroom (~700 MB needed, 1 GB
 * required). Failing fast here is much better UX than ``pip``
 * mid-install dying with a cryptic ENOSPC.
 */
const MIN_BOOTSTRAP_FREE_BYTES = 1024 * 1024 * 1024;

export type ProgressFn = (msg: string) => void;

export interface RuntimeOptions {
  /** Where the cache lives. Pass ``context.globalStorageUri.fsPath``. */
  cacheDir: string;
  /** User-configured python (``emberCode.pythonPath``). Honored if set; bootstrap skipped. */
  configuredPython?: string;
  /** Progress hook for the long-running download/install steps. */
  onProgress?: ProgressFn;
  /** IDE-configured HTTP proxy env vars (HTTPS_PROXY, HTTP_PROXY,
   *  NO_PROXY). Forwarded to uv / pip / the BE so corporate users
   *  whose shell doesn't know about the IDE's proxy still install
   *  cleanly. */
  proxyEnv?: Record<string, string>;
}

/** Result of [ensureBackendPython]: the Python to spawn the BE with,
 *  plus environment variables to layer onto the BE process.
 *  ``HF_HOME`` keeps HuggingFace's cache inside the managed
 *  directory so a clean reinstall really wipes everything.
 *
 *  ``actualCliVersion`` is the ``ember_code.__version__`` reported
 *  by the chosen interpreter, captured at bootstrap time so the
 *  extension can propagate it into the webview URL — the shared
 *  web bundle's ``BackendVersionChip`` reads the values off the
 *  query string and renders a mismatch warning when the installed
 *  CLI drifts from the pinned build. ``null`` on any probe failure.
 */
export type BackendSource = "managed_venv" | "dev_override";

export interface BackendInstall {
  python: string;
  env: Record<string, string>;
  actualCliVersion: string | null;
  expectedCliVersion: string;
  source: BackendSource;
}

/**
 * Resolve a Python interpreter with ``ignite-ember`` installed AND
 * the sentence-transformer embedding model pre-warmed. Bootstraps on
 * first call; returns cached on subsequent calls. Throws if anything
 * goes wrong (caller surfaces via ``vscode.window.showErrorMessage``).
 */
export async function ensureBackendPython(opts: RuntimeOptions): Promise<BackendInstall> {
  const log = opts.onProgress ?? (() => {});

  const proxyEnv = opts.proxyEnv ?? {};
  const expected = IGNITE_EMBER_VERSION;

  // ── Dev / user overrides ──
  // Both ``EMBER_DEV_BACKEND`` and the ``emberCode.pythonPath``
  // setting are opt-in escape hatches for contributors running
  // against an editable checkout. They're deliberately gated on
  // ``IGNITE_EMBER_DEV=1`` so an ambient env var left over from
  // an old shell config (``~/.zshenv``, ``launchctl setenv``, or
  // an OS-level plist) can't silently redirect a regular user
  // to a stale interpreter — the exact footgun that hid a v0.3.8
  // Homebrew CLI behind a v0.8.x plugin.
  const devAck = isDevAcked();
  const devBackend = process.env.EMBER_DEV_BACKEND?.trim();
  const configured = opts.configuredPython?.trim();

  if (devBackend) {
    if (devAck) {
      const actual = await probeCliVersion(devBackend);
      if (actual && actual !== expected) {
        console.warn(
          `EMBER_DEV_BACKEND at ${devBackend} runs ignite-ember ${actual}, ` +
            `plugin pinned to ${expected}. Continuing (dev mode).`,
        );
      }
      return {
        python: devBackend,
        env: { ...proxyEnv },
        actualCliVersion: actual,
        expectedCliVersion: expected,
        source: "dev_override",
      };
    } else {
      console.warn(
        `EMBER_DEV_BACKEND=${devBackend} detected but IGNITE_EMBER_DEV is unset — ` +
          "ignoring override and using the managed venv. " +
          "Set IGNITE_EMBER_DEV=1 to opt in to the dev-mode override.",
      );
    }
  }
  if (configured) {
    if (devAck) {
      const actual = await probeCliVersion(configured);
      return {
        python: configured,
        env: { ...proxyEnv },
        actualCliVersion: actual,
        expectedCliVersion: expected,
        source: "dev_override",
      };
    } else {
      console.warn(
        `emberCode.pythonPath="${configured}" set but IGNITE_EMBER_DEV is unset — ` +
          "ignoring override and using the managed venv.",
      );
    }
  }

  await fs.promises.mkdir(opts.cacheDir, { recursive: true });
  const hfHome = path.join(opts.cacheDir, "huggingface");

  // Disk-space precondition — fail fast with a clear message
  // instead of letting pip / uv die mid-install on ENOSPC.
  await ensureFreeSpace(opts.cacheDir, MIN_BOOTSTRAP_FREE_BYTES, log);

  const uvPath = path.join(opts.cacheDir, isWindows() ? "uv.exe" : "uv");
  const markerPath = path.join(opts.cacheDir, INSTALL_MARKER);
  const venvDir = path.join(opts.cacheDir, "venv");
  const venvPython = path.join(
    venvDir,
    isWindows() ? "Scripts/python.exe" : "bin/python",
  );

  const wantMarker = JSON.stringify({
    uv: UV_VERSION,
    python: PYTHON_VERSION,
    ignite: IGNITE_EMBER_VERSION,
  });
  const haveMarker = await readFileOrNull(markerPath);
  const markerMatches = haveMarker === wantMarker;

  // Probe the venv's interpreter to catch a specific failure
  // mode: marker file says one version, but the wheels on disk
  // are a different version (manual pip upgrade, half-finished
  // install, extension update that skipped the marker rewrite).
  // Only act on a positive mismatch — probe returned a version
  // AND it differs. Null probe = interpreter missing or wedged;
  // fall back to marker/executable signals so a transient
  // subprocess hiccup doesn't trigger a multi-minute reinstall
  // on every startup.
  const venvActualVersion = (await isExecutable(venvPython))
    ? await probeCliVersion(venvPython)
    : null;
  const venvVersionMismatch =
    venvActualVersion !== null && venvActualVersion !== IGNITE_EMBER_VERSION;
  if (venvVersionMismatch && markerMatches) {
    console.warn(
      `Managed venv marker says ignite=${IGNITE_EMBER_VERSION} but the ` +
        `interpreter reports ${venvActualVersion} — reinstalling.`,
    );
  }
  const needsReinstall =
    !(await isExecutable(venvPython)) || !markerMatches || venvVersionMismatch;

  // ── 1. uv binary ──
  if (!(await isExecutable(uvPath)) || needsReinstall) {
    log("Downloading uv (one-time, ~25 MB)…");
    await downloadUv(uvPath, proxyEnv);
  }

  // ── 2. Python + 3. venv + 4. ignite-ember + 5. prefetch ──
  if (needsReinstall) {
    if (await pathExists(venvDir)) {
      log("Refreshing managed venv…");
      await fs.promises.rm(venvDir, { recursive: true, force: true });
    }
    log(`Installing Python ${PYTHON_VERSION} (one-time)…`);
    await runUv(uvPath, ["python", "install", PYTHON_VERSION], proxyEnv);

    log("Creating backend venv…");
    await runUv(uvPath, ["venv", "--python", PYTHON_VERSION, venvDir], proxyEnv);

    log("Installing ignite-ember (one-time)…");
    await runUv(uvPath, [
      "pip",
      "install",
      "--python",
      venvPython,
      `ignite-ember==${IGNITE_EMBER_VERSION}`,
    ], proxyEnv);

    // Pre-warm the sentence-transformer cache so the first agent
    // run doesn't stall mid-chat on a silent 90 MB HuggingFace
    // download.
    log("Downloading embedding model (one-time, ~90 MB)…");
    await runProcess(venvPython, ["-m", "ember_code.prefetch_models"], {
      env: { HF_HOME: hfHome, ...proxyEnv },
      timeoutMs: 10 * 60_000,
    });

    await fs.promises.writeFile(markerPath, wantMarker);
  }

  // Probe the (possibly-just-reinstalled) venv one more time so
  // the returned ``BackendInstall`` carries the confirmed version.
  // Skipped when the initial probe already matched and no
  // reinstall happened.
  const finalVersion = needsReinstall
    ? await probeCliVersion(venvPython)
    : venvActualVersion;

  return {
    python: venvPython,
    env: { HF_HOME: hfHome, ...proxyEnv },
    actualCliVersion: finalVersion,
    expectedCliVersion: expected,
    source: "managed_venv",
  };
}

/** ``true`` when ``IGNITE_EMBER_DEV`` is set to a truthy value.
 *  Accepts ``1`` or a case-insensitive ``true``; everything else
 *  (including unset) means "override not acknowledged". */
function isDevAcked(): boolean {
  const v = process.env.IGNITE_EMBER_DEV;
  return v === "1" || (typeof v === "string" && v.toLowerCase() === "true");
}

/** Return ``ember_code.__version__`` as reported by the given
 *  Python interpreter, or ``null`` on any failure. 2s subprocess
 *  timeout — the real observed cost for a cold import is 30-80ms;
 *  the ceiling is a safety net against a wedged interpreter
 *  blocking the whole bootstrap. Exposed so the extension's
 *  "Diagnose Backend" command (and future callers) can reuse it
 *  without duplicating the subprocess wiring.
 */
export async function probeCliVersion(python: string): Promise<string | null> {
  if (!python || !(await isExecutable(python))) return null;
  try {
    const { spawn } = await import("child_process");
    return await new Promise<string | null>((resolve) => {
      const proc = spawn(
        python,
        ["-c", "import ember_code, sys; sys.stdout.write(ember_code.__version__)"],
        { stdio: ["ignore", "pipe", "ignore"] },
      );
      let out = "";
      const timer = setTimeout(() => {
        proc.kill("SIGKILL");
        resolve(null);
      }, 2000);
      proc.stdout?.on("data", (chunk: Buffer) => {
        out += chunk.toString();
      });
      proc.on("error", () => {
        clearTimeout(timer);
        resolve(null);
      });
      proc.on("close", (code: number | null) => {
        clearTimeout(timer);
        if (code !== 0) return resolve(null);
        const trimmed = out.trim();
        resolve(trimmed.length > 0 ? trimmed : null);
      });
    });
  } catch {
    return null;
  }
}

/**
 * Throw with a clear message if the filesystem holding ``dir``
 * doesn't have at least ``minBytes`` free. ``dir`` may not exist
 * yet; we walk up to an existing ancestor.
 */
async function ensureFreeSpace(
  dir: string,
  minBytes: number,
  log: ProgressFn,
): Promise<void> {
  // Node's ``fs.statfs`` returns block-level info we can multiply
  // out to bytes. Walk up until we hit an existing path so the
  // call doesn't error on a not-yet-created cache dir.
  let probe = dir;
  while (probe && !(await pathExists(probe))) {
    const parent = path.dirname(probe);
    if (parent === probe) break;
    probe = parent;
  }
  let free: number;
  try {
    const st = await fs.promises.statfs(probe);
    free = Number(st.bavail) * Number(st.bsize);
  } catch {
    // statfs unavailable on this Node build — skip the check
    // rather than blocking installation.
    return;
  }
  if (free >= minBytes) return;
  const freeMb = Math.round(free / (1024 * 1024));
  const needMb = Math.round(minBytes / (1024 * 1024));
  log("Disk space check failed.");
  throw new Error(
    `Not enough disk space for the Ember backend bootstrap: ` +
      `${freeMb} MB free at ${dir}, need at least ${needMb} MB. ` +
      `Free up space and try again.`,
  );
}

/** Wipe the entire managed cache. Used by ``emberCode.reinstall``. */
export async function resetCache(cacheDir: string): Promise<void> {
  if (await pathExists(cacheDir)) {
    await fs.promises.rm(cacheDir, { recursive: true, force: true });
  }
}

// ── Platform + downloads ───────────────────────────────────────────

function isWindows(): boolean {
  return process.platform === "win32";
}

/** GitHub-release asset triple for the current OS/arch. */
function uvTarget(): string {
  const arch = process.arch;
  switch (process.platform) {
    case "darwin":
      return arch === "arm64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
    case "linux":
      return arch === "arm64" ? "aarch64-unknown-linux-gnu" : "x86_64-unknown-linux-gnu";
    case "win32":
      return "x86_64-pc-windows-msvc";
    default:
      throw new Error(`Unsupported platform: ${process.platform}/${arch}`);
  }
}

async function downloadUv(
  dest: string,
  proxyEnv: Record<string, string> = {},
): Promise<void> {
  const triple = uvTarget();
  const ext = isWindows() ? "zip" : "tar.gz";
  const url =
    `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${triple}.${ext}`;
  const tmp = path.join(os.tmpdir(), `uv-${Date.now()}.${ext}`);

  // ``fetch`` itself doesn't read HTTPS_PROXY in Node's built-in
  // implementation. The cleanest way to honour the IDE proxy
  // without pulling in undici as a dep is to fall back to ``curl``
  // when proxy env is set — curl is on every supported platform
  // (including modern Windows) and honors *_PROXY out of the box.
  // For the no-proxy case we keep fetch (faster, no subprocess).
  try {
    if (proxyEnv.HTTPS_PROXY || proxyEnv.HTTP_PROXY) {
      await runProcess(
        "curl",
        ["-fsSL", "--retry", "3", "-o", tmp, url],
        { env: proxyEnv, timeoutMs: 5 * 60_000 },
      );
    } else {
      await downloadFile(url, tmp);
    }
    await extractUv(tmp, dest, ext);
  } finally {
    fs.promises.unlink(tmp).catch(() => {});
  }
}

async function downloadFile(url: string, dest: string): Promise<void> {
  // Follow redirects manually — GitHub releases redirect via 302
  // to a signed S3 URL, and Node's ``fetch`` follows by default
  // since 18 but the typing isn't always reliable in our @types
  // version. Using ``fetch`` is the simplest path.
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok || !res.body) {
    throw new Error(`uv download failed: HTTP ${res.status} from ${url}`);
  }
  const file = fs.createWriteStream(dest);
  // Stream the response body to disk. Node 18+ ``Response.body`` is
  // a web ReadableStream; ``pipeline`` from node:stream/promises
  // handles the conversion.
  const reader = (res.body as ReadableStream<Uint8Array>).getReader();
  await new Promise<void>((resolve, reject) => {
    file.on("error", reject);
    file.on("finish", resolve);
    (async () => {
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!file.write(Buffer.from(value))) {
            await new Promise<void>((r) => file.once("drain", r));
          }
        }
        file.end();
      } catch (e) {
        file.destroy(e as Error);
      }
    })();
  });
}

async function extractUv(archive: string, dest: string, ext: string): Promise<void> {
  await fs.promises.mkdir(path.dirname(dest), { recursive: true });
  if (ext === "zip") {
    // Windows zip — defer to PowerShell's Expand-Archive to avoid a
    // zip-lib dependency. Extracts to a temp dir, then we move
    // ``uv.exe`` into place.
    const extractDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "uv-zip-"));
    try {
      await runProcess(
        "powershell.exe",
        ["-NoProfile", "-Command", `Expand-Archive -Path '${archive}' -DestinationPath '${extractDir}' -Force`],
      );
      const found = await findFileNamed(extractDir, "uv.exe");
      if (!found) throw new Error("uv.exe not found in archive");
      await fs.promises.rename(found, dest);
    } finally {
      await fs.promises.rm(extractDir, { recursive: true, force: true });
    }
  } else {
    // tar.gz on macOS/Linux. ``tar`` is universally available.
    const extractDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "uv-tar-"));
    try {
      await runProcess("tar", ["xzf", archive, "-C", extractDir]);
      const found = await findFileNamed(extractDir, "uv");
      if (!found) throw new Error("uv binary not found in archive");
      await fs.promises.rename(found, dest);
      await fs.promises.chmod(dest, 0o755);
    } finally {
      await fs.promises.rm(extractDir, { recursive: true, force: true });
    }
  }
}

async function findFileNamed(root: string, name: string): Promise<string | null> {
  const entries = await fs.promises.readdir(root, { withFileTypes: true });
  for (const e of entries) {
    const full = path.join(root, e.name);
    if (e.isFile() && e.name === name) return full;
    if (e.isDirectory()) {
      const nested = await findFileNamed(full, name);
      if (nested) return nested;
    }
  }
  return null;
}

// ── uv / process invocation ────────────────────────────────────────

function runUv(
  uvPath: string,
  args: string[],
  proxyEnv: Record<string, string> = {},
): Promise<void> {
  return runProcess(uvPath, args, { timeoutMs: 10 * 60_000, env: proxyEnv });
}

function runProcess(
  cmd: string,
  args: string[],
  opts: { timeoutMs?: number; env?: Record<string, string> } = {},
): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: opts.env ? { ...process.env, ...opts.env } : process.env,
    });
    let stderr = "";
    child.stdout?.on("data", () => {}); // drain
    child.stderr?.on("data", (b) => {
      stderr = (stderr + b.toString()).slice(-4096);
    });
    const timer = opts.timeoutMs
      ? setTimeout(() => {
          child.kill("SIGKILL");
          reject(new Error(`${cmd} ${args.join(" ")} timed out`));
        }, opts.timeoutMs)
      : null;
    child.on("error", (e) => {
      if (timer) clearTimeout(timer);
      reject(e);
    });
    child.on("exit", (code) => {
      if (timer) clearTimeout(timer);
      if (code === 0) resolve();
      else reject(new Error(`${cmd} exited ${code}: ${stderr.trim()}`));
    });
  });
}

// ── Filesystem helpers ─────────────────────────────────────────────

async function pathExists(p: string): Promise<boolean> {
  try {
    await fs.promises.stat(p);
    return true;
  } catch {
    return false;
  }
}

async function isExecutable(p: string): Promise<boolean> {
  try {
    const st = await fs.promises.stat(p);
    return st.isFile();
  } catch {
    return false;
  }
}

async function readFileOrNull(p: string): Promise<string | null> {
  try {
    return await fs.promises.readFile(p, "utf8");
  } catch {
    return null;
  }
}

