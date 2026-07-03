/**
 * igni VSCode extension.
 *
 * Hosts the shared web UI (clients/web, bundled into ./media) in a
 * webview panel and spawns the Python backend for the open workspace.
 * The backend interpreter comes from a managed venv the extension
 * provisions on first launch via uv — the user never has to ``pip
 * install ignite-ember``. See ``runtime.ts`` for the bootstrap.
 *
 * Host bridge (the ``host.*`` calls the shared web UI emits) is
 * fully implemented:
 *
 *   ember:openFile         → showTextDocument with optional line range selection
 *   ember:notify           → showInformationMessage
 *   ember:fileEdited       → revert/reload the open document (Local History captures it)
 *   ember:searchCode       → indexed substring search via workspace.findFiles + read
 *
 * And the reverse direction — IDE → web UI — fires:
 *
 *   ember:addToComposer    → "Add Selection to Ember Chat" editor action
 *   ember:attachFile       → "Add File to Ember Chat" explorer action
 *   ember:theme            → IDE theme change pushed into the webview
 */

import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
import { ensureBackendPython, probeCliVersion, resetCache } from "./runtime";
import { IGNITE_EMBER_VERSION } from "./version.generated";

let backend: ChildProcess | undefined;
let backendPort: number | undefined;
/** Version info captured at bootstrap so ``buildHtml`` can splice
 *  it into ``<meta>`` tags for the shared ``BackendVersionChip``
 *  in the web bundle. Undefined until the first backend start
 *  succeeds; ``buildHtml`` renders no chip when it's absent. */
let backendVersionInfo:
  | { actual: string | null; expected: string; source: string }
  | undefined;
let panel: vscode.WebviewPanel | undefined;

// ``<project>/.ember/backend.lock`` — see the Python side at
// ``src/ember_code/backend/lockfile.py`` for the write half and the
// shape spec.
interface LockfilePayload {
  pid: number;
  port: number;
  wire_version: string;
  created_at: number;
}

type DiscoverResult =
  | { status: "ok"; port: number }
  | { status: "spawn" }
  | { status: "version_mismatch"; runningVersion: string };

function isPidAlive(pid: number): boolean {
  if (pid <= 0) return false;
  try {
    // ``kill(pid, 0)`` is the canonical no-op probe: succeeds if the
    // process exists, throws ESRCH if not, EPERM if it exists but
    // we can't signal it (still counts as alive for our purpose —
    // same-user processes never hit EPERM).
    process.kill(pid, 0);
    return true;
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "EPERM") return true;
    return false;
  }
}

function isPortReachable(port: number, host = "127.0.0.1", timeoutMs = 500): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let settled = false;
    const finish = (result: boolean) => {
      if (settled) return;
      settled = true;
      sock.destroy();
      resolve(result);
    };
    sock.setTimeout(timeoutMs);
    sock.once("connect", () => finish(true));
    sock.once("timeout", () => finish(false));
    sock.once("error", () => finish(false));
    sock.connect(port, host);
  });
}

async function discoverExistingBackend(
  projectDir: string,
  expectedWireVersion: string,
): Promise<DiscoverResult> {
  const lockPath = path.join(projectDir, ".ember", "backend.lock");
  let raw: string;
  try {
    raw = await fs.promises.readFile(lockPath, "utf-8");
  } catch {
    return { status: "spawn" };
  }
  let payload: LockfilePayload;
  try {
    payload = JSON.parse(raw);
  } catch {
    // Corrupted lockfile — treat as stale, spawn.
    await fs.promises.unlink(lockPath).catch(() => undefined);
    return { status: "spawn" };
  }
  if (!isPidAlive(payload.pid)) {
    await fs.promises.unlink(lockPath).catch(() => undefined);
    return { status: "spawn" };
  }
  if (!(await isPortReachable(payload.port))) {
    await fs.promises.unlink(lockPath).catch(() => undefined);
    return { status: "spawn" };
  }
  if (payload.wire_version !== expectedWireVersion) {
    // Keep the lockfile — the running BE is legitimately owned.
    // Surface upstream so the user gets a notification.
    return { status: "version_mismatch", runningVersion: payload.wire_version };
  }
  return { status: "ok", port: payload.port };
}

function startBackend(
  context: vscode.ExtensionContext,
  projectDir: string,
  progress: (msg: string) => void,
): Promise<number> {
  return (async () => {
    const configured = vscode.workspace
      .getConfiguration("emberCode")
      .get<string>("pythonPath", "")
      .trim();

    progress("Preparing Ember backend…");
    const install = await ensureBackendPython({
      cacheDir: context.globalStorageUri.fsPath,
      configuredPython: configured || undefined,
      proxyEnv: vscodeProxyEnv(),
      onProgress: progress,
    });
    backendVersionInfo = {
      actual: install.actualCliVersion,
      expected: install.expectedCliVersion,
      source: install.source,
    };

    // Check for a running backend on this project (another IDE
    // window / another instance of this extension). When one is
    // found and healthy, reuse its port instead of spawning a
    // duplicate — both clients then talk to the same Python
    // process and see each other's chat updates live. See
    // ``src/ember_code/backend/lockfile.py`` for the write side.
    const discovered = await discoverExistingBackend(projectDir, install.expectedCliVersion);
    if (discovered.status === "ok") {
      progress(`Reusing running Ember backend on port ${discovered.port}`);
      return discovered.port;
    }
    if (discovered.status === "version_mismatch") {
      // Refuse to connect — the running BE speaks a different wire
      // version and mixing traffic would corrupt state. Surface to
      // the user; they can close the other window (or restart it
      // to bring it in sync) and reopen igni.
      const msg =
        `Another igni instance is running for this project on ` +
        `version ${discovered.runningVersion}, but this client is ` +
        `${install.expectedCliVersion}. Close the other client (or ` +
        `restart it on the matching version) and reopen igni.`;
      vscode.window.showErrorMessage(msg);
      throw new Error(msg);
    }
    // ``status === "spawn"`` — either no lockfile or the recorded
    // BE is dead. Fall through to the normal spawn path.

    progress("Starting Ember backend…");
    return new Promise<number>((resolve, reject) => {
      const child = spawn(
        install.python,
        ["-m", "ember_code.backend", "--ws-port", "0", "--project-dir", projectDir],
        {
          // ``install.env`` carries HF_HOME (and any future runtime
          // env) so the BE uses the managed HuggingFace cache and a
          // clean Reinstall really wipes embeddings too.
          env: {
            ...process.env,
            ...install.env,
            EMBER_PARENT_PID: String(process.pid),
          },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

      const timer = setTimeout(() => {
        child.kill();
        reject(new Error("Ember backend did not become ready within 120s"));
      }, 120_000);

      let buf = "";
      let stderrTail = "";
      child.stdout.on("data", (chunk: Buffer) => {
        buf += chunk.toString();
        let nl: number;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try {
            const obj = JSON.parse(line);
            if (obj.status === "ready" && obj.ws_port) {
              clearTimeout(timer);
              backend = child;
              backendPort = obj.ws_port;
              resolve(obj.ws_port);
              return;
            }
          } catch {
            /* non-JSON startup noise (warnings) — skip */
          }
        }
      });
      child.stderr.on("data", (chunk: Buffer) => {
        stderrTail = (stderrTail + chunk.toString()).slice(-4096);
      });
      child.on("error", (err) => {
        clearTimeout(timer);
        reject(
          new Error(
            `Failed to spawn '${install.python}': ${err.message}\n` +
              `Try igni: Reinstall Backend.`,
          ),
        );
      });
      child.on("exit", (code) => {
        clearTimeout(timer);
        if (backendPort === undefined) {
          const tail = stderrTail.trim();
          const detail = tail ? `\n\nstderr:\n${tail}` : "";
          reject(
            new Error(
              `Ember backend exited during startup (code ${code}).\n` +
                `Python used: ${install.python}` +
                detail,
            ),
          );
        }
        backend = undefined;
        backendPort = undefined;
      });
    });
  })();
}

function buildHtml(webview: vscode.Webview, extensionUri: vscode.Uri, wsPort: number): string {
  const mediaRoot = vscode.Uri.joinPath(extensionUri, "media");
  let html = fs.readFileSync(path.join(mediaRoot.fsPath, "index.html"), "utf8");

  html = html.replace(
    /(src|href)="\.\/(assets\/[^"]+)"/g,
    (_m, attr: string, p: string) =>
      `${attr}="${webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, p))}"`,
  );

  const csp = [
    `default-src 'none'`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `script-src ${webview.cspSource}`,
    `font-src ${webview.cspSource}`,
    `img-src ${webview.cspSource} data:`,
    `connect-src ws://127.0.0.1:${wsPort}`,
  ].join("; ");

  // Version chip in the web bundle reads these <meta> tags (see
  // ``BackendVersionChip`` / ``readBackendVersionParams``). Same
  // three values the JB plugin passes via URL query params —
  // VSCode webviews can't reliably use query params under the
  // default CSP, so we deliver them here instead. Absent info
  // (extension hot-reloaded before the first bootstrap finished)
  // means no chip renders, which is fine.
  const versionMetas = backendVersionInfo
    ? [
        `<meta name="ember-expected-cli" content="${escapeAttr(backendVersionInfo.expected)}">`,
        `<meta name="ember-actual-cli" content="${escapeAttr(backendVersionInfo.actual ?? "unknown")}">`,
        `<meta name="ember-backend-source" content="${escapeAttr(backendVersionInfo.source)}">`,
      ].join("\n")
    : "";

  html = html.replace(
    "<head>",
    `<head>\n<meta http-equiv="Content-Security-Policy" content="${csp}">` +
      `\n<meta name="ember-ws-url" content="ws://127.0.0.1:${wsPort}">` +
      (versionMetas ? `\n${versionMetas}` : ""),
  );
  return html;
}

/** HTML-attribute-safe escape for values we splice into ``<meta
 *  content="…">``. The values we're spliceing are semver-shaped
 *  strings + a short enum, so realistic attackers get nothing here;
 *  the escape is belt-and-suspenders in case a future rebrand
 *  lets funkier characters through. */
function escapeAttr(v: string): string {
  return v
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Indexed code search via VSCode's workspace search APIs. Used when
 * the shared web UI posts ``ember:searchCode`` (paste-handler in the
 * composer). VSCode doesn't ship a trigram index like PyCharm's, but
 * ``findFiles`` honors ``.gitignore`` and ``files.exclude``, and
 * scanning text from the VSCode-cached document model is cheap.
 *
 * Returns the same shape as the WS ``search_code`` RPC so the FE
 * consumer doesn't have to branch.
 */
async function searchCode(snippet: string): Promise<{
  matches: { path: string; line: number; end_line: number; preview: string }[];
  truncated: boolean;
}> {
  if (!snippet || snippet.length < 5) return { matches: [], truncated: false };
  const workspace = vscode.workspace.workspaceFolders?.[0];
  if (!workspace) return { matches: [], truncated: false };

  const snippetLines = (snippet.match(/\n/g) ?? []).length + 1;
  const firstNonBlank =
    snippet.split("\n").find((l) => l.trim().length > 0)?.trim() ?? snippet;
  const previewLine = firstNonBlank;

  // Hard cap so a huge workspace doesn't make the paste handler
  // hang. ``findFiles`` already honors search-exclude settings.
  const files = await vscode.workspace.findFiles(
    "**/*",
    "**/{node_modules,dist,build,.git,.venv,venv,target}/**",
    2000,
  );
  const maxResults = 20;
  const matches: {
    path: string;
    line: number;
    end_line: number;
    preview: string;
  }[] = [];

  for (const uri of files) {
    if (matches.length >= maxResults) break;
    try {
      const stat = await vscode.workspace.fs.stat(uri);
      if (stat.size > 2 * 1024 * 1024) continue;
      const bytes = await vscode.workspace.fs.readFile(uri);
      const text = Buffer.from(bytes).toString("utf8");
      const idx = text.indexOf(snippet);
      if (idx < 0) continue;
      const line = text.slice(0, idx).split("\n").length;
      const rel = path.relative(workspace.uri.fsPath, uri.fsPath);
      matches.push({
        path: rel,
        line,
        end_line: line + snippetLines - 1,
        preview: previewLine,
      });
    } catch {
      // unreadable / binary — skip
    }
  }
  return { matches, truncated: false };
}

/**
 * Wire the host-bridge messages the shared web UI emits.
 *
 * The web client's ``host.*`` methods (clients/web/src/lib/host.ts)
 * dispatch ``postMessage({type, ...})`` whenever a VSCode webview is
 * detected. Without this listener those messages silently disappear.
 */
function registerHostBridge(p: vscode.WebviewPanel, context: vscode.ExtensionContext) {
  p.webview.onDidReceiveMessage(async (msg: { type?: string }) => {
    if (!msg || typeof msg.type !== "string") return;
    try {
      switch (msg.type) {
        case "ember:openFile":
          await handleOpenFile(p, msg);
          return;
        case "ember:notify": {
          const { title, body } = msg as { title?: string; body?: string };
          const text = [title, body].filter(Boolean).join(" — ");
          if (text) vscode.window.showInformationMessage(text);
          return;
        }
        case "ember:fileEdited": {
          // The backend's edit tools just wrote to disk. If an
          // editor tab has the file open, reload it from disk so
          // the user sees the new content (and Local History
          // captures the change via VSCode's file-watcher path —
          // the same hook the "modified externally" prompt uses).
          const rawPath = (msg as { path?: string }).path;
          if (!rawPath) return;
          const abs = resolveAbs(rawPath);
          if (!abs) return;
          const uri = vscode.Uri.file(abs);
          const doc = vscode.workspace.textDocuments.find(
            (d) => d.uri.fsPath === uri.fsPath,
          );
          if (doc) {
            // ``revert`` re-reads disk into the open document. No
            // explicit show — preserve the user's current focus.
            await vscode.commands.executeCommand("workbench.action.files.revert", uri);
          }
          return;
        }
        case "ember:searchCode": {
          const snippet = (msg as { snippet?: string }).snippet ?? "";
          const requestId = (msg as { id?: string | number }).id ?? null;
          const result = await searchCode(snippet);
          // Reply via a paired ``ember:searchCodeResult`` so the FE
          // can correlate. The web client's ``host.searchCode``
          // pairs request/response by id.
          p.webview.postMessage({
            type: "ember:searchCodeResult",
            id: requestId,
            result,
          });
          return;
        }
      }
    } catch (e) {
      vscode.window.showErrorMessage(`Ember bridge: ${String(e)}`);
    }
  });
  // Wire theme bridge — push current + on change.
  pushTheme(p);
  context.subscriptions.push(
    vscode.window.onDidChangeActiveColorTheme(() => pushTheme(p)),
  );
}

function pushTheme(p: vscode.WebviewPanel) {
  const kind = vscode.window.activeColorTheme.kind;
  const dark =
    kind === vscode.ColorThemeKind.Dark || kind === vscode.ColorThemeKind.HighContrast;
  p.webview.postMessage({ type: "ember:theme", dark });
}

async function handleOpenFile(p: vscode.WebviewPanel, msg: { type?: string }) {
  const raw = (msg as { path?: string }).path;
  if (!raw) return;
  // ``<path>:<start>[-<end>]`` — start/end optional. Anchored regex
  // so Windows paths like ``C:\foo`` aren't truncated.
  const m = raw.match(/^(.*?):(\d+)(?:-(\d+))?$/);
  const filePart = m ? m[1] : raw;
  const startLine = m ? Math.max(0, Number(m[2]) - 1) : null;
  const endLine = m && m[3] ? Math.max(0, Number(m[3]) - 1) : null;
  const abs = resolveAbs(filePart);
  if (!abs) return;

  const webviewCol = p.viewColumn ?? vscode.ViewColumn.Two;
  const editorCol =
    vscode.window.activeTextEditor?.viewColumn ??
    vscode.window.visibleTextEditors.find((ed) => ed.viewColumn !== webviewCol)
      ?.viewColumn ??
    (webviewCol === vscode.ViewColumn.One ? vscode.ViewColumn.Two : vscode.ViewColumn.One);

  const opts: vscode.TextDocumentShowOptions = {
    preview: false,
    viewColumn: editorCol,
  };
  if (startLine !== null) {
    const start = new vscode.Position(startLine, 0);
    const end =
      endLine !== null
        ? new vscode.Position(endLine, Number.MAX_SAFE_INTEGER)
        : start;
    opts.selection = new vscode.Range(start, end);
  }
  await vscode.window.showTextDocument(vscode.Uri.file(abs), opts);
}

function resolveAbs(p: string): string | null {
  if (path.isAbsolute(p)) return p;
  const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  return ws ? path.join(ws, p) : null;
}

/**
 * Pick up the IDE's HTTP proxy (Settings → Application → Proxy or
 * the standard ``http.proxy`` setting) and emit
 * ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY`` env vars the
 * spawned uv + pip + Python subprocesses honor. VSCode reads its
 * own proxy from these env vars first, then from the
 * ``http.proxy`` setting — we forward both so a corporate user
 * whose shell doesn't know the proxy still gets a working install.
 *
 * Returns ``{}`` when no proxy is configured.
 */
function vscodeProxyEnv(): Record<string, string> {
  const out: Record<string, string> = {};
  // ``WorkspaceConfiguration.get`` doesn't necessarily return a
  // string even when the setting is declared as one — when the user
  // hasn't set it, ``proxySupport: "off"`` returns ``null``, and
  // unset registered settings can come back as ``undefined``. Be
  // defensive about the type before calling ``.trim()``.
  const readStr = (key: string): string => {
    const raw = vscode.workspace.getConfiguration("http").get(key);
    return typeof raw === "string" ? raw.trim() : "";
  };
  const proxy = readStr("proxy");
  if (proxy) {
    out.HTTPS_PROXY = proxy;
    out.HTTP_PROXY = proxy;
  } else {
    // Fall back to the shell env in case the user set them outside
    // the IDE settings.
    if (process.env.HTTPS_PROXY) out.HTTPS_PROXY = process.env.HTTPS_PROXY;
    if (process.env.HTTP_PROXY) out.HTTP_PROXY = process.env.HTTP_PROXY;
  }
  const noProxy = readStr("noProxy") || process.env.NO_PROXY || "";
  if (noProxy) out.NO_PROXY = noProxy;
  return out;
}

function pushToComposer(payload: {
  text: string;
  path?: string;
  line?: number;
  end_line?: number;
}) {
  if (!panel) {
    vscode.commands.executeCommand("emberCode.open").then(() => {
      // After the panel opens, deliver the event.
      setTimeout(() => panel?.webview.postMessage({ type: "ember:addToComposer", payload }), 300);
    });
    return;
  }
  panel.reveal();
  panel.webview.postMessage({ type: "ember:addToComposer", payload });
}

function attachFile(path: string) {
  if (!panel) {
    vscode.commands.executeCommand("emberCode.open").then(() => {
      setTimeout(
        () => panel?.webview.postMessage({ type: "ember:attachFile", payload: { path } }),
        300,
      );
    });
    return;
  }
  panel.reveal();
  panel.webview.postMessage({ type: "ember:attachFile", payload: { path } });
}

export function activate(context: vscode.ExtensionContext) {
  context.subscriptions.push(
    // ── Open / show the chat panel ─────────────────────────────
    vscode.commands.registerCommand("emberCode.open", async () => {
      if (panel) {
        panel.reveal();
        return;
      }
      const folder =
        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();

      let port: number;
      try {
        port =
          backendPort ??
          (await vscode.window.withProgress(
            {
              location: vscode.ProgressLocation.Notification,
              title: "igni",
            },
            (p) =>
              startBackend(context, folder, (msg) => p.report({ message: msg })),
          ));
      } catch (e) {
        vscode.window.showErrorMessage(String(e));
        return;
      }

      panel = vscode.window.createWebviewPanel(
        "emberCode",
        "igni",
        vscode.ViewColumn.Beside,
        {
          enableScripts: true,
          retainContextWhenHidden: true,
          localResourceRoots: [vscode.Uri.joinPath(context.extensionUri, "media")],
        },
      );
      panel.iconPath = vscode.Uri.joinPath(context.extensionUri, "icon.png");
      panel.webview.html = buildHtml(panel.webview, context.extensionUri, port);
      registerHostBridge(panel, context);
      panel.onDidDispose(() => {
        panel = undefined;
      });
    }),

    // ── Editor → chat ──────────────────────────────────────────
    vscode.commands.registerCommand("emberCode.addSelectionToChat", () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const sel = editor.selection;
      const doc = editor.document;
      let range: vscode.Range;
      if (!sel.isEmpty) {
        range = new vscode.Range(sel.start, sel.end);
      } else {
        const line = doc.lineAt(sel.active.line);
        range = line.range;
      }
      const text = doc.getText(range);
      const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const rel = ws ? path.relative(ws, doc.uri.fsPath) : doc.uri.fsPath;
      pushToComposer({
        text,
        path: rel,
        line: range.start.line + 1,
        end_line: range.end.line + 1,
      });
    }),

    // ── Explorer → chat (single or multi-select) ──────────────
    vscode.commands.registerCommand(
      "emberCode.addFileToChat",
      (single: vscode.Uri | undefined, multi: vscode.Uri[] | undefined) => {
        const targets = multi && multi.length ? multi : single ? [single] : [];
        const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        for (const uri of targets) {
          const rel = ws ? path.relative(ws, uri.fsPath) : uri.fsPath;
          attachFile(rel);
        }
      },
    ),

    // ── Restart backend (preserve cache) ──────────────────────
    vscode.commands.registerCommand("emberCode.restart", async () => {
      backend?.kill();
      backend = undefined;
      backendPort = undefined;
      vscode.window.showInformationMessage("Ember backend killed. Reopening will respawn it.");
      if (panel) {
        panel.dispose();
      }
      await vscode.commands.executeCommand("emberCode.open");
    }),

    // ── Reinstall backend (wipe cache, redownload everything) ─
    vscode.commands.registerCommand("emberCode.reinstall", async () => {
      const confirm = await vscode.window.showWarningMessage(
        "Wipe the managed Python cache and re-download uv + Python + ignite-ember?",
        { modal: true },
        "Reinstall",
      );
      if (confirm !== "Reinstall") return;
      backend?.kill();
      backend = undefined;
      backendPort = undefined;
      if (panel) panel.dispose();
      await resetCache(context.globalStorageUri.fsPath);
      await vscode.commands.executeCommand("emberCode.open");
    }),

    // ── Diagnose backend ────────────────────────────────────────
    // Bug-triage dump — plugin/CLI versions, interpreter path,
    // dev-override state, marker contents. Written to an
    // ``igni: Diagnostics`` output channel (multi-line native
    // dialogs on macOS collapse whitespace) and also placed on
    // the clipboard so the user can paste it into a bug report
    // without transcription.
    vscode.commands.registerCommand("emberCode.doctor", async () => {
      const report = await buildDiagnosticReport(context);
      const channel = getDiagnosticsChannel();
      channel.clear();
      channel.appendLine(report);
      channel.show(true);
      try {
        await vscode.env.clipboard.writeText(report);
        vscode.window.showInformationMessage(
          "Diagnostic report copied to clipboard.",
        );
      } catch {
        // Clipboard failed — the channel view is still useful.
      }
    }),
  );
}

let diagnosticsChannel: vscode.OutputChannel | undefined;
function getDiagnosticsChannel(): vscode.OutputChannel {
  diagnosticsChannel ??= vscode.window.createOutputChannel("igni: Diagnostics");
  return diagnosticsChannel;
}

async function buildDiagnosticReport(
  context: vscode.ExtensionContext,
): Promise<string> {
  const cacheDir = context.globalStorageUri.fsPath;
  const isWin = process.platform === "win32";
  const venvPython = path.join(
    cacheDir,
    "venv",
    isWin ? "Scripts/python.exe" : "bin/python",
  );
  const markerPath = path.join(cacheDir, "ember-install.json");

  const configured = vscode.workspace
    .getConfiguration("emberCode")
    .get<string>("pythonPath", "")
    .trim();
  const devBackend = process.env.EMBER_DEV_BACKEND?.trim();
  const devAck = process.env.IGNITE_EMBER_DEV;
  const devActive =
    devAck === "1" ||
    (typeof devAck === "string" && devAck.toLowerCase() === "true");

  const activePython =
    devActive && devBackend
      ? devBackend
      : devActive && configured
        ? configured
        : venvPython;
  const actual = (await probeCliVersion(activePython)) ?? "<probe failed>";
  const expected = IGNITE_EMBER_VERSION;

  let markerContents: string;
  try {
    markerContents = (await fs.promises.readFile(markerPath, "utf8")).trim();
  } catch {
    markerContents = "<missing>";
  }

  let venvPresent = false;
  try {
    const stat = await fs.promises.stat(venvPython);
    venvPresent = stat.isFile();
  } catch {
    venvPresent = false;
  }

  const lines: string[] = [];
  lines.push("igni VSCode extension · backend diagnostics");
  lines.push("──────────────────────────────────────────");
  lines.push(`Extension version        : ${expected}`);
  lines.push(`Expected ignite-ember    : ${expected}`);
  lines.push(`Actual ignite-ember      : ${actual}`);
  if (actual !== expected && actual !== "<probe failed>") {
    lines.push("                           ↑ MISMATCH — chat may fail");
  }
  lines.push("");
  const usedOverride =
    devActive && (!!devBackend || !!configured);
  lines.push(
    `Backend source           : ${usedOverride ? "dev override" : "managed venv"}`,
  );
  lines.push(`Interpreter path         : ${activePython}`);
  lines.push(`Managed venv path        : ${venvPython}`);
  lines.push(`Managed venv present     : ${venvPresent}`);
  lines.push("");
  lines.push(`EMBER_DEV_BACKEND        : ${devBackend ?? "<unset>"}`);
  lines.push(`emberCode.pythonPath     : ${configured || "<unset>"}`);
  lines.push(`IGNITE_EMBER_DEV         : ${devAck ?? "<unset>"}`);
  if ((devBackend || configured) && !devActive) {
    lines.push(
      "                           ↑ override set without ack — ignored",
    );
  }
  lines.push("");
  lines.push(`Marker file              : ${markerPath}`);
  lines.push(`Marker contents          : ${markerContents}`);
  return lines.join("\n");
}

export function deactivate() {
  backend?.kill();
  backend = undefined;
  backendPort = undefined;
}
