import { resolve as resolvePath } from "node:path";
/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
      "@ignite-ember/ui-react/": resolvePath(import.meta.dirname, "../../packages/ui-react/src") + "/",
      "@ignite-ember/ui-react": resolvePath(
        import.meta.dirname,
        "../../packages/ui-react/src/index.ts",
      ),
      "@ignite-ember/test-utils/": resolvePath(import.meta.dirname, "../../packages/test-utils") + "/",
      // The floor's peer dependencies, pointed at this package's
      // ``node_modules``. ``packages/`` is outside it, so a bare
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
      "@ignite-ember/extension-bridge/": resolvePath(import.meta.dirname, "../../packages/extension-bridge/src") + "/",
      "@ignite-ember/extension-bridge": resolvePath(import.meta.dirname, "../../packages/extension-bridge/src/index.ts"),
      "@ignite-ember/ui-css": resolvePath(import.meta.dirname, "../ui/igni-ui.css"),
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
    fs: { allow: [resolvePath(import.meta.dirname, "../..")] },
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
    // ``npx vitest run --coverage`` (CI's Linux web job). Every source
    // file counts, loaded by a test or not: without ``include`` v8 only
    // reports the files some test imported, so ``App.tsx`` and four
    // settings panels at 0% were simply absent and the total read 69%
    // instead of 47%. The packages the app is built from are measured
    // too — they sit outside this root, hence ``allowExternal``.
    coverage: {
      provider: "v8",
      allowExternal: true,
      include: ["src/**/*.{ts,tsx}", "**/packages/*/src/**/*.{ts,tsx}"],
      exclude: ["**/node_modules/**", "**/*.test.*", "**/*.spec.*", "**/*.generated.ts", "**/*.d.ts"],
      reporter: ["text-summary"],
      // The floor: statements / branches / functions / lines when it was
      // set (68.7 / 61.9 / 65.0 / 70.6), minus one point, so the number
      // can only go up. Raise it when coverage rises; never lower it to
      // let a change through.
      thresholds: {
        statements: 67.7,
        branches: 60.9,
        functions: 64,
        lines: 69.6,
      },
    },
  },
});
