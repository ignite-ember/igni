import { resolve as resolvePath } from "node:path";
/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the built assets load from file:// (Tauri),
// vscode-webview:// and jbcefbrowser:// origins, not just http.
export default defineConfig({
  resolve: {
    alias: {
      // The floor is its own package, so it can be published and so
      // that an import of it from the app goes through the same
      // specifier an extension author writes. An alias rather than a
      // workspace link because the app is built, tested and shipped
      // from here and a link would make `npm install` a build step.
      "@igni/ui-react/": resolvePath(__dirname, "../../packages/ui-react/src") + "/",
      "@igni/ui-react": resolvePath(
        __dirname,
        "../../packages/ui-react/src/index.ts",
      ),
      "@igni/test-utils/": resolvePath(__dirname, "../../packages/test-utils") + "/",
      // The floor's peer dependencies, pointed at this package's
      // ``node_modules``. ``packages/`` is outside it, so a bare
      // import from there resolves nowhere — the same hole the
      // tsconfig ``paths`` cover for the type-checker. A real npm
      // workspace would fix both at once and is the right end state;
      // it also makes ``npm install`` a build step for the app,
      // which is a bigger change than this one.
      react: resolvePath(__dirname, "./node_modules/react"),
      "react-dom": resolvePath(__dirname, "./node_modules/react-dom"),
      "react/jsx-runtime": resolvePath(__dirname, "./node_modules/react/jsx-runtime"),
      "react-markdown": resolvePath(__dirname, "./node_modules/react-markdown"),
      "remark-gfm": resolvePath(__dirname, "./node_modules/remark-gfm"),
      mermaid: resolvePath(__dirname, "./node_modules/mermaid"),
      "@testing-library/react": resolvePath(
        __dirname,
        "./node_modules/@testing-library/react",
      ),
      "@igni/extension-bridge/": resolvePath(__dirname, "../../packages/extension-bridge/src") + "/",
      "@igni/extension-bridge": resolvePath(__dirname, "../../packages/extension-bridge/src/index.ts"),
      "@igni/ui-css": resolvePath(__dirname, "../ui/igni-ui.css"),
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
    // ``packages/`` is a sibling of ``clients/``, and vite refuses to
    // serve outside its root by default.
    fs: { allow: [resolvePath(__dirname, "../..")] },
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
    // The floor's tests moved out with the floor. They are still this
    // suite's business — `styled.test.ts` and `boundary.test.ts` are
    // what stop a published component shipping unstyled or dragging
    // the app behind it — so they are pulled back in by path rather
    // than left to a second runner nobody would remember to run.
    include: [
      "src/**/*.{test,spec}.{ts,tsx}",
      "../../packages/**/*.{test,spec}.{ts,tsx}",
    ],
  },
});
