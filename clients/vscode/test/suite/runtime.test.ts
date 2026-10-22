/**
 * Regression tests for the two places the extension used to lie to
 * the user about the backend.
 *
 *   * SWU-087 / SWU-092 — "Diagnose Backend" re-derived the
 *     interpreter precedence instead of asking the code that
 *     launches the backend, so with ``igni.pythonPath`` set it named
 *     the managed venv and called the (honored) override "ignored".
 *     The precedence now lives once, in ``runtime.resolveInterpreter``.
 *
 *   * SWU-091 — ``igni.restart`` announced "backend killed" even when
 *     this window had reused a backend discovered through
 *     ``.igni/backend.lock`` and therefore killed nothing.
 *
 * Both fixes are exercised through the compiled output rather than
 * the sources: this file's program has ``rootDir: test``, so ``src``
 * can't be imported from it. ``out/runtime.js`` and
 * ``out/extension.js`` are what the extension host actually loads.
 */

import * as assert from "assert";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

const OUT_ROOT = path.resolve(__dirname, "..", "..");

// Same derivation as extension.test.ts — the publisher/name pair is
// read from package.json so a rename can't quietly break the lookup.
const PKG_PATH = path.join(__dirname, "..", "..", "..", "package.json");
const pkg = JSON.parse(fs.readFileSync(PKG_PATH, "utf8")) as {
  publisher: string;
  name: string;
};
const PUBLISHER_ID = `${pkg.publisher}.${pkg.name}`;

type InterpreterOrigin = "dev_env" | "configured_setting" | "managed_venv";

interface InterpreterResolution {
  python: string;
  origin: InterpreterOrigin;
  source: "managed_venv" | "dev_override";
  managedVenvPython: string;
  ignoredDevBackend?: string;
}

interface RuntimeModule {
  resolveInterpreter(opts: {
    cacheDir: string;
    configuredPython?: string;
  }): InterpreterResolution;
}

interface ExtensionModule {
  planRestart(state: { ownsBackend: boolean; reusedPort?: number }): {
    outcome: "killed" | "not_owned" | "nothing_running";
    message: string;
  };
  buildDiagnosticReport(context: vscode.ExtensionContext): Promise<string>;
}

// eslint-disable-next-line @typescript-eslint/no-var-requires
const runtime = require(path.join(OUT_ROOT, "runtime.js")) as RuntimeModule;
// eslint-disable-next-line @typescript-eslint/no-var-requires
const extension = require(path.join(OUT_ROOT, "extension.js")) as ExtensionModule;

/** A path that is guaranteed not to exist, so the version probe in
 *  the diagnostic report fails fast instead of spawning anything. */
const ABSENT_PYTHON = path.join(os.tmpdir(), "igni-test-absent-python");

function setEnv(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}

suite("interpreter resolution (SWU-087 / SWU-092)", () => {
  const savedDevBackend = process.env.IGNI_DEV_BACKEND;
  const savedDevAck = process.env.IGNITE_EMBER_DEV;

  setup(() => {
    delete process.env.IGNI_DEV_BACKEND;
    delete process.env.IGNITE_EMBER_DEV;
  });

  suiteTeardown(() => {
    setEnv("IGNI_DEV_BACKEND", savedDevBackend);
    setEnv("IGNITE_EMBER_DEV", savedDevAck);
  });

  test("falls back to the managed venv when nothing overrides it", () => {
    const r = runtime.resolveInterpreter({ cacheDir: "/tmp/igni-cache" });
    assert.strictEqual(r.origin, "managed_venv");
    assert.strictEqual(r.source, "managed_venv");
    assert.strictEqual(r.python, r.managedVenvPython);
    assert.strictEqual(r.ignoredDevBackend, undefined);
    assert.ok(
      r.managedVenvPython.startsWith(path.join("/tmp/igni-cache", "venv")),
      `managed venv path not under the cache dir: ${r.managedVenvPython}`,
    );
  });

  test("igni.pythonPath is honored without the dev-mode ack", () => {
    // The whole of SWU-092: the setting needs no ``IGNITE_EMBER_DEV``
    // ack, and the backend really does run from it.
    const r = runtime.resolveInterpreter({
      cacheDir: "/tmp/igni-cache",
      configuredPython: "/custom/venv/bin/python",
    });
    assert.strictEqual(r.python, "/custom/venv/bin/python");
    assert.strictEqual(r.origin, "configured_setting");
    assert.strictEqual(r.source, "dev_override");
    assert.strictEqual(r.ignoredDevBackend, undefined);
    assert.notStrictEqual(r.python, r.managedVenvPython);
  });

  test("blank / whitespace igni.pythonPath is not an override", () => {
    const r = runtime.resolveInterpreter({
      cacheDir: "/tmp/igni-cache",
      configuredPython: "   ",
    });
    assert.strictEqual(r.origin, "managed_venv");
    assert.strictEqual(r.python, r.managedVenvPython);
  });

  test("IGNI_DEV_BACKEND without the ack is the only genuinely ignored override", () => {
    process.env.IGNI_DEV_BACKEND = "/stale/python";
    const r = runtime.resolveInterpreter({
      cacheDir: "/tmp/igni-cache",
      configuredPython: "/custom/venv/bin/python",
    });
    assert.strictEqual(r.python, "/custom/venv/bin/python");
    assert.strictEqual(r.origin, "configured_setting");
    assert.strictEqual(r.ignoredDevBackend, "/stale/python");
  });

  test("an acked IGNI_DEV_BACKEND outranks igni.pythonPath", () => {
    process.env.IGNI_DEV_BACKEND = "/dev/python";
    process.env.IGNITE_EMBER_DEV = "1";
    const r = runtime.resolveInterpreter({
      cacheDir: "/tmp/igni-cache",
      configuredPython: "/custom/venv/bin/python",
    });
    assert.strictEqual(r.python, "/dev/python");
    assert.strictEqual(r.origin, "dev_env");
    assert.strictEqual(r.source, "dev_override");
    assert.strictEqual(r.ignoredDevBackend, undefined);
  });

  test("the doctor reports the interpreter the runtime resolved", async () => {
    const cfg = vscode.workspace.getConfiguration("igni");
    const saved = cfg.get<string>("pythonPath");
    await cfg.update(
      "pythonPath",
      ABSENT_PYTHON,
      vscode.ConfigurationTarget.Global,
    );
    const cacheDir = await fs.promises.mkdtemp(
      path.join(os.tmpdir(), "igni-doctor-"),
    );
    try {
      // Only ``globalStorageUri`` is read by the report; a stub keeps
      // the test out of the real extension's cache directory.
      const context = {
        globalStorageUri: vscode.Uri.file(cacheDir),
      } as unknown as vscode.ExtensionContext;
      const report = await extension.buildDiagnosticReport(context);

      const resolved = runtime.resolveInterpreter({
        cacheDir,
        configuredPython: ABSENT_PYTHON,
      });
      assert.strictEqual(
        resolved.python,
        ABSENT_PYTHON,
        "precondition: the runtime should resolve to the configured path",
      );
      assert.ok(
        report.includes(`Interpreter path         : ${resolved.python}`),
        `report named a different interpreter than the runtime resolves:\n${report}`,
      );
      assert.ok(
        report.includes("Backend source           : user override (igni.pythonPath)"),
        `report did not attribute the backend to igni.pythonPath:\n${report}`,
      );
      assert.ok(
        !/ignored/.test(report),
        `report called an honored override "ignored":\n${report}`,
      );
    } finally {
      await cfg.update("pythonPath", saved, vscode.ConfigurationTarget.Global);
      await fs.promises.rm(cacheDir, { recursive: true, force: true });
    }
  });

  test("the doctor still flags an unacked IGNI_DEV_BACKEND as ignored", async () => {
    process.env.IGNI_DEV_BACKEND = "/stale/python";
    const cacheDir = await fs.promises.mkdtemp(
      path.join(os.tmpdir(), "igni-doctor-"),
    );
    try {
      const context = {
        globalStorageUri: vscode.Uri.file(cacheDir),
      } as unknown as vscode.ExtensionContext;
      const report = await extension.buildDiagnosticReport(context);
      assert.ok(
        /IGNI_DEV_BACKEND set without the IGNITE_EMBER_DEV ack — ignored/.test(report),
        `report did not flag the unacked env override:\n${report}`,
      );
      assert.ok(
        !report.includes("Interpreter path         : /stale/python"),
        `report claimed the ignored override was in use:\n${report}`,
      );
    } finally {
      await fs.promises.rm(cacheDir, { recursive: true, force: true });
    }
  });
});

suite("restart ownership (SWU-091)", () => {
  test("a backend this window spawned is killed, and says so", () => {
    const plan = extension.planRestart({ ownsBackend: true });
    assert.strictEqual(plan.outcome, "killed");
    assert.match(plan.message, /killed/i);
  });

  test("a reused backend is not claimed as killed", () => {
    const plan = extension.planRestart({ ownsBackend: false, reusedPort: 51234 });
    assert.strictEqual(plan.outcome, "not_owned");
    assert.doesNotMatch(
      plan.message,
      /killed/i,
      "restart claimed a kill it cannot perform",
    );
    assert.match(plan.message, /51234/, "message should name the port in use");
    assert.match(plan.message, /did not start/i);
  });

  test("nothing running is reported as nothing running", () => {
    const plan = extension.planRestart({ ownsBackend: false });
    assert.strictEqual(plan.outcome, "nothing_running");
    assert.doesNotMatch(plan.message, /killed/i);
  });

  // The live half: with the fake backend the extension spawns (and so
  // owns) the process, so the user-visible notification must be the
  // "killed" one and no warning. Windows can't fake-spawn the BE —
  // see the note in extension.test.ts.
  test("igni.restart on an owned backend shows the kill notification", async function () {
    if (process.platform === "win32") {
      this.skip();
    }
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    assert.ok(ext, `extension ${PUBLISHER_ID} not found in test host`);
    await ext!.activate();

    const fakeBe = path.join(ext!.extensionPath, "test", "fake-backend.js");
    const cfg = vscode.workspace.getConfiguration("igni");
    const saved = cfg.get<string>("pythonPath");
    await cfg.update("pythonPath", fakeBe, vscode.ConfigurationTarget.Global);

    const infos: string[] = [];
    const warnings: string[] = [];
    const origInfo = vscode.window.showInformationMessage;
    const origWarn = vscode.window.showWarningMessage;
    (vscode.window as any).showInformationMessage = (msg: string) => {
      infos.push(msg);
      return Promise.resolve(undefined);
    };
    (vscode.window as any).showWarningMessage = (msg: string) => {
      warnings.push(msg);
      return Promise.resolve(undefined);
    };
    try {
      // First restart normalises whatever state earlier suites left
      // behind: it disposes any panel and its trailing ``igni.open``
      // spawns a fresh backend this window owns.
      await vscode.commands.executeCommand("igni.restart");
      infos.length = 0;
      warnings.length = 0;
      await vscode.commands.executeCommand("igni.restart");
      assert.ok(
        infos.some((m) => /killed/i.test(m)),
        `expected a kill notification for an owned backend; got: ${infos.join(" | ") || "(none)"}`,
      );
      assert.deepStrictEqual(
        warnings,
        [],
        "owned restart should not warn about foreign ownership",
      );
    } finally {
      (vscode.window as any).showInformationMessage = origInfo;
      (vscode.window as any).showWarningMessage = origWarn;
      await cfg.update("pythonPath", saved, vscode.ConfigurationTarget.Global);
    }
  });
});
