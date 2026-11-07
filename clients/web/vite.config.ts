import { resolve as resolvePath } from "node:path";
/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// igni's SDKs — the floor (`@ignite-ember/ui-react`), the bridge and the
// stylesheet — are igni-sdk's, built from its checkout's source beside this
// repository (`IGNI_SDK_DIR`, or `../igni-sdk`). tsconfig.json's `paths`
// name the same place; they cannot read the environment, so the default is
// the layout CI checks out too.
const IGNI_SDK = process.env.IGNI_SDK_DIR ?? resolvePath(import.meta.dirname, "../../../igni-sdk");

// Relative base so the built assets load from file:// (Tauri),
// not just http.
export default defineConfig({
  resolve: {
    alias: {
      // The floor is its own package, so it can be published and so
      // that an import of it from the app goes through the same
      // specifier an extension author writes. An alias rather than a
      // workspace link because the app is built, tested and shipped
      // from here and a link would make `npm install` a build step.
      "@ignite-ember/ui-react/": resolvePath(IGNI_SDK, "packages/ui-react/src") + "/",
      "@ignite-ember/ui-react": resolvePath(IGNI_SDK, "packages/ui-react/src/index.ts"),
      "@ignite-ember/test-utils/": resolvePath(IGNI_SDK, "packages/test-utils") + "/",
      // The floor's peer dependencies, pointed at this package's
      // ``node_modules``. igni-sdk's ``packages/`` is outside it, so a bare
      // import from there resolves nowhere — the same hole the
      // tsconfig ``paths`` cover for the type-checker. A real npm
      // workspace would fix both at once and is the right end state;
      // it also makes ``npm install`` a build step for the app,
      // which is a bigger change than this one.
      react: resolvePath(import.meta.dirname, "./node_modules/react"),
      "react-dom": resolvePath(import.meta.dirname, "./node_modules/react-dom"),
      "react/jsx-runtime": resolvePath(import.meta.dirname, "./node_modules/react/jsx-runtime"),
      "react-markdown": resolvePath(import.meta.dirname, "./node_modules/react-markdown"),
      "remark-gfm": resolvePath(import.meta.dirname, "./node_modules/remark-gfm"),
      mermaid: resolvePath(import.meta.dirname, "./node_modules/mermaid"),
      "@testing-library/react": resolvePath(
        import.meta.dirname,
        "./node_modules/@testing-library/react",
      ),
      "@ignite-ember/extension-bridge/": resolvePath(IGNI_SDK, "packages/extension-bridge/src") + "/",
      "@ignite-ember/extension-bridge": resolvePath(IGNI_SDK, "packages/extension-bridge/src/index.ts"),
      "@ignite-ember/ui-css": resolvePath(IGNI_SDK, "clients/ui/igni-ui.css"),
    },
  },
  plugins: [react()],
  base: "./",
  // Pin to the port that ``clients/tauri/src-tauri/tauri.conf.json``
  // expects in ``devUrl`` — otherwise vite picks 5173 by default and
  // ``cargo tauri dev`` times out waiting on 5179.
  server: {
    port: 5179,
    strictPort: true,
    // igni-sdk's checkout is outside this root, and vite refuses to serve
    // outside its root by default.
    fs: { allow: [resolvePath(import.meta.dirname, "../.."), IGNI_SDK] },
  },
  build: {
    outDir: "dist",
    target: "es2022",
  },
  test: {
    // Vitest auto-discovers ``**/*.{test,spec}.ts``. Without this
    // exclude it picks up our Playwright e2e files under ``e2e/``,
    // which import ``@playwright/test`` and crash with
    // "test.describe() called in a configuration file". Playwright
    // has its own runner (``npx playwright test``); keep the two
    // worlds separate.
    exclude: ["node_modules", "dist", "e2e", "test-results", "playwright-report"],
    // The SDKs' own tests — the floor's `styled.test.ts` and
    // `boundary.test.ts` among them — run in igni-sdk, whose CI holds the
    // stylesheet to this app's theme.css.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // ``npx vitest run --coverage`` (CI's Linux web job). Every source
    // file counts, loaded by a test or not: without ``include`` v8 only
    // reports the files some test imported, so ``App.tsx`` and four
    // settings panels at 0% were simply absent and the total read 69%
    // instead of 47%. The SDKs this app is built from are measured in
    // igni-sdk, not here.
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["**/node_modules/**", "**/*.test.*", "**/*.spec.*", "**/*.generated.ts", "**/*.d.ts"],
      reporter: ["text-summary"],
      // The floor: statements / branches / functions / lines when it was
      // set, minus one point, so the number can only go up. Raise it when
      // coverage rises; never lower it to let a change through.
      //
      // Set again when the SDKs moved to igni-sdk (66.9 / 60.3 / 62.7 /
      // 68.7, the app alone): the old floor (68.7 / 61.9 / 65.0 / 70.6)
      // measured the app and the SDK packages together, and the packages,
      // better covered, are measured in igni-sdk now with a floor of their
      // own. Nothing here lost a test.
      thresholds: {
        statements: 65.9,
        branches: 59.3,
        functions: 61.7,
        lines: 67.7,
      },
    },
  },
});
