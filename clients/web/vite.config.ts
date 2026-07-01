/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the built assets load from file:// (Tauri),
// vscode-webview:// and jbcefbrowser:// origins, not just http.
export default defineConfig({
  plugins: [react()],
  base: "./",
  // Pin to the port that ``clients/tauri/src-tauri/tauri.conf.json``
  // expects in ``devUrl`` — otherwise vite picks 5173 by default and
  // ``cargo tauri dev`` times out waiting on 5179.
  server: { port: 5179, strictPort: true },
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
  },
});
