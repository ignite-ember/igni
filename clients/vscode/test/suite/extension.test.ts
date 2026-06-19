/**
 * Smoke tests for the Ember Code VSCode extension.
 *
 * These run inside the extension host process (via
 * ``@vscode/test-electron``) and assert on the IDE-side surface:
 * activation, command registration, panel mount. Webview DOM is
 * covered by the web e2e suite — the webview is a sandboxed iframe
 * the extension host can't introspect.
 *
 * The real BE bootstrap (uv → Python → ignite-ember, ~30 s) is
 * bypassed by pointing ``emberCode.pythonPath`` at a fake Node
 * script that just prints the ready JSON. The fake doesn't expose
 * a WS port, so the webview falls back to "Connecting…" — fine
 * for these tests.
 */

import * as assert from "assert";
import * as path from "path";
import * as vscode from "vscode";

const PUBLISHER_ID = "ignite-ember.ember-code-vscode";

suite("Ember Code extension", () => {
  suiteSetup(async () => {
    // Anchor the fake "Python" path on the extension's install
    // directory (the source tree, not the compiled-output mirror)
    // so we don't have to copy fake-backend.js into ``out/``.
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    assert.ok(ext, `extension ${PUBLISHER_ID} not found in test host`);
    const fakeBe = path.join(ext!.extensionPath, "test", "fake-backend.js");
    await vscode.workspace
      .getConfiguration("emberCode")
      .update("pythonPath", fakeBe, vscode.ConfigurationTarget.Global);
  });

  suiteTeardown(async () => {
    await vscode.workspace
      .getConfiguration("emberCode")
      .update("pythonPath", undefined, vscode.ConfigurationTarget.Global);
  });

  test("extension is present and can be activated", async () => {
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    assert.ok(ext, `extension ${PUBLISHER_ID} not found`);
    await ext!.activate();
    assert.ok(ext!.isActive, "extension failed to activate");
  });

  test("all contributed commands are registered", async () => {
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    await ext!.activate();
    const all = await vscode.commands.getCommands(true);
    const expected = [
      "emberCode.open",
      "emberCode.addSelectionToChat",
      "emberCode.addFileToChat",
      "emberCode.restart",
      "emberCode.reinstall",
    ];
    for (const cmd of expected) {
      assert.ok(
        all.includes(cmd),
        `command ${cmd} not registered (registered: ${all.filter((c) => c.startsWith("emberCode")).join(", ")})`,
      );
    }
  });

  test("emberCode.open creates the chat panel", async () => {
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    await ext!.activate();

    // Capture error notifications so we can include them in the
    // assertion message — extension swallows BE-spawn failures into
    // ``showErrorMessage`` and returns early without a panel; that
    // would otherwise look like a silent test failure.
    const errors: string[] = [];
    const orig = vscode.window.showErrorMessage;
    (vscode.window as any).showErrorMessage = (msg: string) => {
      errors.push(msg);
      // Don't forward — the modal would block the test. We only
      // need the message text for diagnostics.
      return Promise.resolve(undefined);
    };

    try {
      await vscode.commands.executeCommand("emberCode.open");

      // Poll for the tab to land in the tab-groups model.
      const deadline = Date.now() + 5_000;
      let emberTab: vscode.Tab | undefined;
      while (Date.now() < deadline) {
        emberTab = vscode.window.tabGroups.all
          .flatMap((g) => g.tabs)
          .find((t) => t.label === "Ember Code");
        if (emberTab) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.ok(
        emberTab,
        `Ember Code panel tab not found within 5 s. Errors: ${errors.join(" | ") || "(none)"}`,
      );
    } finally {
      (vscode.window as any).showErrorMessage = orig;
    }
  });

  test("addSelectionToChat is a no-op when no editor is active", async () => {
    // The command should not crash when invoked without an active
    // text editor. It just returns silently. We don't have a way to
    // open a text editor easily in this minimal test harness, so we
    // assert the no-active-editor branch is clean.
    await vscode.commands.executeCommand("emberCode.addSelectionToChat");
    // If we get here without throwing, the test passes.
    assert.ok(true);
  });

  test("addSelectionToChat with an active editor + selection", async () => {
    // Happy-path counterpart to the no-editor case above. Opens a
    // throwaway untitled document, places a selection, runs the
    // command; verifies the Ember panel ends up active (the command
    // calls ``pushToComposer`` which reveals/opens the panel) and no
    // error is shown. We don't introspect the webview message —
    // ``panel.webview.postMessage`` is module-internal and not
    // observable from the extension host — but the panel side-effect
    // alone catches the most likely regression class: code path
    // throwing on a non-empty selection, or panel never being told
    // to open. Caught in earlier iterations: an off-by-one on
    // ``range.end.line + 1`` that crashed on empty docs.
    const doc = await vscode.workspace.openTextDocument({
      content: "first line\nsecond line\nthird line\n",
      language: "plaintext",
    });
    const editor = await vscode.window.showTextDocument(doc);
    // Select "second line".
    editor.selection = new vscode.Selection(
      new vscode.Position(1, 0),
      new vscode.Position(1, "second line".length),
    );

    const errors: string[] = [];
    const orig = vscode.window.showErrorMessage;
    (vscode.window as any).showErrorMessage = (msg: string) => {
      errors.push(msg);
      return Promise.resolve(undefined);
    };
    try {
      await vscode.commands.executeCommand("emberCode.addSelectionToChat");
      // pushToComposer opens the panel if not already open; give the
      // tab-groups model a beat to register.
      const deadline = Date.now() + 3_000;
      let emberTab: vscode.Tab | undefined;
      while (Date.now() < deadline) {
        emberTab = vscode.window.tabGroups.all
          .flatMap((g) => g.tabs)
          .find((t) => t.label === "Ember Code");
        if (emberTab) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.ok(
        emberTab,
        `Ember panel was not opened by addSelectionToChat. Errors: ${errors.join(" | ") || "(none)"}`,
      );
      assert.deepStrictEqual(
        errors,
        [],
        "addSelectionToChat surfaced an error on the happy path",
      );
    } finally {
      (vscode.window as any).showErrorMessage = orig;
    }
  });

  test("addFileToChat tolerates missing arguments", async () => {
    // The command is wired to the explorer/context menu, which
    // passes a Uri. When invoked from the command palette (no args),
    // it should bail out cleanly.
    await vscode.commands.executeCommand("emberCode.addFileToChat");
    assert.ok(true);
  });

  test("addFileToChat with a single Uri attaches without error", async () => {
    // Happy-path counterpart: the explorer's "Add File to Ember
    // Chat" right-click action passes a single ``Uri``. Use the
    // extension's own ``package.json`` as the file — guaranteed to
    // exist in the test sandbox and harmless to "attach" (the
    // webview is a "Connecting…" placeholder with the fake BE, so
    // the message is consumed but no chat action fires).
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    const fileUri = vscode.Uri.file(
      path.join(ext!.extensionPath, "package.json"),
    );

    const errors: string[] = [];
    const orig = vscode.window.showErrorMessage;
    (vscode.window as any).showErrorMessage = (msg: string) => {
      errors.push(msg);
      return Promise.resolve(undefined);
    };
    try {
      // VSCode forwards explorer-action arguments positionally; the
      // signature is ``(single, multi)`` so passing just one Uri
      // mirrors the right-click case (no multi-select).
      await vscode.commands.executeCommand(
        "emberCode.addFileToChat",
        fileUri,
      );
      assert.deepStrictEqual(
        errors,
        [],
        "addFileToChat(single Uri) surfaced an error on the happy path",
      );
    } finally {
      (vscode.window as any).showErrorMessage = orig;
    }
  });

  test("addFileToChat with a multi-select Uri[] attaches all", async () => {
    // Explorer multi-select hands the second arg an array of Uris;
    // ``targets`` should resolve to the multi array. Three files
    // pinpoints the iteration path.
    const ext = vscode.extensions.getExtension(PUBLISHER_ID);
    const root = ext!.extensionPath;
    const uris = [
      vscode.Uri.file(path.join(root, "package.json")),
      vscode.Uri.file(path.join(root, "tsconfig.json")),
      vscode.Uri.file(path.join(root, ".vscodeignore")),
    ];

    const errors: string[] = [];
    const orig = vscode.window.showErrorMessage;
    (vscode.window as any).showErrorMessage = (msg: string) => {
      errors.push(msg);
      return Promise.resolve(undefined);
    };
    try {
      // First arg is the "primary" Uri, second arg is the array —
      // matches what VSCode passes for multi-select context menus.
      await vscode.commands.executeCommand(
        "emberCode.addFileToChat",
        uris[0],
        uris,
      );
      assert.deepStrictEqual(
        errors,
        [],
        "addFileToChat(multi Uris) surfaced an error",
      );
    } finally {
      (vscode.window as any).showErrorMessage = orig;
    }
  });

  test("emberCode.open with invalid pythonPath surfaces a clean error", async () => {
    // Users mistype or move their venv and ``pythonPath`` becomes a
    // dead pointer. The spawn path must fail loud with a readable
    // message via ``showErrorMessage`` — not crash the extension
    // host, not silently leave the panel hung at "Connecting…"
    // forever.
    const cfg = vscode.workspace.getConfiguration("emberCode");
    const saved = cfg.get<string>("pythonPath");
    await cfg.update(
      "pythonPath",
      "/definitely/not/a/real/python/binary",
      vscode.ConfigurationTarget.Global,
    );

    const errors: string[] = [];
    const orig = vscode.window.showErrorMessage;
    (vscode.window as any).showErrorMessage = (msg: string) => {
      errors.push(msg);
      return Promise.resolve(undefined);
    };
    try {
      // Force a fresh spawn: kill any existing BE first.
      await vscode.commands.executeCommand("emberCode.restart").then(
        () => {},
        () => {},
      );
      await vscode.commands.executeCommand("emberCode.open");

      // Poll for the error to surface — spawn fails fast on macOS
      // (ENOENT) but reach for a generous deadline so this isn't
      // flaky on CI.
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline && errors.length === 0) {
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.ok(
        errors.length > 0,
        "expected a showErrorMessage call with the BE-spawn failure; got none",
      );
      const combined = errors.join("\n");
      // The exact wording can shift over time; pin only the
      // identifying tokens so harmless rephrasings don't break the
      // test.
      assert.ok(
        /not.*real.*python|spawn|backend|Failed/i.test(combined),
        `error message didn't mention the bad path or spawn failure: ${combined}`,
      );
    } finally {
      (vscode.window as any).showErrorMessage = orig;
      await cfg.update(
        "pythonPath",
        saved,
        vscode.ConfigurationTarget.Global,
      );
    }
  });
});
