/**
 * Test driver: downloads a real VSCode binary, launches it with our
 * extension loaded, and runs the Mocha test suite inside the
 * extension host. This is the standard ``@vscode/test-electron``
 * pattern — there's no real alternative for testing extensions, since
 * the IDE's APIs only exist inside the extension host process.
 *
 * Invoked by ``npm run test:host``.
 */

import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main() {
  try {
    const extensionDevelopmentPath = path.resolve(__dirname, "../..");
    const extensionTestsPath = path.resolve(__dirname, "./suite/index");

    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      // Pin to a VSCode build that ships a layout @vscode/test-electron
      // 3.x can actually spawn on macOS-arm64 GitHub runners.
      // 1.131.0 changed the macOS arm64 helper layout (puts Electron
      // at a path test-electron 3.x doesn't probe), so spawning the
      // downloaded bundle fails with ``Electron ENOENT``. 1.129.0
      // is the last build with the original layout — keep this
      // pinned until upstream test-electron catches up. (Ubuntu and
      // Windows runners use their own native layout, so the pin only
      // affects the macos-latest job.)
      version: "1.129.0",
      // ``--disable-extensions`` keeps the test environment hermetic;
      // we don't want the user's locally-installed extensions
      // affecting activation events or commands. ``--user-data-dir``
      // points at a throwaway dir so this run doesn't pollute the
      // user's real VSCode settings.
      launchArgs: ["--disable-extensions"],
    });
  } catch (err) {
    console.error("Failed to run tests:", err);
    process.exit(1);
  }
}

main();
