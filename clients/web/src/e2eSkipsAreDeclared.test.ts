/**
 * F128. The e2e suite reported "41 passed" while skipping seven tests,
 * and those seven were the only ones that drove the app against a real
 * backend. Each sat behind an environment variable — two different
 * ones — that nothing set. CI had been told about one of them and not
 * the other, and no output anywhere said the difference.
 *
 * ``e2e/reporters/skips.ts`` catches that at run time. This catches it
 * at ``npm test`` time, which matters because Playwright only runs on
 * Linux in CI while vitest runs on every host in the matrix, and
 * because the reporter is silent on a machine where nothing happens to
 * skip.
 *
 * The rule is about the *decision*, not the mechanism: a spec may not
 * decide whether to run by reading an environment variable unless the
 * test that depends on it carries a tag declaring the skip. Wanting a
 * URL from the environment is fine. Quietly not running is not.
 */

import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const E2E_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "e2e");

/** Tags that declare a skip. Must match ``e2e/reporters/skips.ts``. */
const DECLARING_TAGS = ["@manual", "@needs-baseline"];

function specFiles(): string[] {
  return readdirSync(E2E_DIR)
    .filter((f) => f.endsWith(".spec.ts"))
    .sort();
}

const read = (f: string) => readFileSync(join(E2E_DIR, f), "utf8");

describe("the e2e suite cannot skip quietly", () => {
  it("finds specs to check", () => {
    // Without this the whole file passes vacuously if the directory
    // moves — the failure mode that made F121's dataset useless.
    const files = specFiles();
    expect(files.length).toBeGreaterThanOrEqual(10);
    expect(files).toContain("real-be.spec.ts");
  });

  it("every spec that can skip declares it with a tag", () => {
    const offenders: string[] = [];
    for (const f of specFiles()) {
      const src = read(f);
      if (!/\btest\.skip\s*\(/.test(src)) continue;
      // Either quote style. A rule that depends on formatting is a
      // rule that fires on the formatter — this one first failed on a
      // spec tagged `{ tag: '@manual' }` in single quotes.
      const declares = DECLARING_TAGS.some(
        (t) => src.includes(`tag: "${t}"`) || src.includes(`tag: '${t}'`),
      );
      if (!declares) offenders.push(f);
    }
    expect(
      offenders,
      `these specs can skip but no test in them carries one of ` +
        `${DECLARING_TAGS.join("/")}. Either make them runnable — ` +
        `e2e/fixtures/live-be.ts spawns a real backend whenever the ` +
        `project venv exists — or declare the skip.`,
    ).toEqual([]);
  });

  it("no spec gates itself on an IGNI_* variable of its own invention", () => {
    // The two that caused F128 were IGNI_E2E_REAL_BE and IGNI_LIVE_WS.
    // The first is gone. The second survives as an *override* — point
    // a spec at a backend you already have — and lives in one place,
    // the fixture, where it is documented and where a missing value
    // falls through to spawning rather than to skipping.
    // IGNI_E2E_SHOTS gates the screenshot generator, which is tagged
    // `@manual` and declares its skip — the thing this rule is for is
    // coverage going quiet, not a generator that writes files for a
    // person to look at.
    // IGNI_E2E_A11Y_BOTH *adds* the light theme to the accessibility
    // sweep; the dark one runs unconditionally. This rule is about
    // coverage going quiet, not about an opt-in second pass.
    const allowed = new Set([
      "IGNI_LIVE_WS",
      "IGNI_E2E_STRICT",
      "IGNI_E2E_SHOTS",
      "IGNI_E2E_A11Y_BOTH",
    ]);
    const found = new Map<string, string[]>();
    for (const f of specFiles()) {
      for (const m of read(f).matchAll(/process\.env\.(IGNI_[A-Z0-9_]+)/g)) {
        if (allowed.has(m[1])) continue;
        found.set(m[1], [...(found.get(m[1]) ?? []), f]);
      }
    }
    expect(
      Object.fromEntries(found),
      "a new IGNI_* gate in a spec is how F128 happened twice. Put the " +
        "decision in e2e/fixtures/live-be.ts, where it is a capability " +
        "check rather than a flag nobody sets.",
    ).toEqual({});
  });

  it("the reporter is wired into the Playwright config", () => {
    // A guard that nothing runs is worse than no guard — F126's dead
    // indentation check. Pin the wiring, not just the file's existence.
    const cfg = readFileSync(
      join(E2E_DIR, "..", "playwright.config.ts"),
      "utf8",
    );
    expect(cfg).toContain("./e2e/reporters/skips.ts");
  });

  it("the e2e directory is type-checked", () => {
    // Playwright transpiles specs without type-checking them, so if
    // ``tsc`` does not read this directory nothing does — which was the
    // case until F128, and the first check found four errors including
    // a listener typed against the wrong event. A spec that skips *and*
    // is unchecked is invisible twice over.
    const cfg = readFileSync(
      join(E2E_DIR, "..", "tsconfig.json"),
      "utf8",
    );
    const include = JSON.parse(
      cfg.replace(/\/\*[\s\S]*?\*\//g, ""),
    ).include as string[];
    expect(include).toContain("e2e");
    expect(include).toContain("src");
  });

  it("the reporter and this test agree on what a declaring tag is", () => {
    const reporter = readFileSync(
      join(E2E_DIR, "reporters", "skips.ts"),
      "utf8",
    );
    for (const tag of DECLARING_TAGS) {
      expect(reporter).toContain(`"${tag}"`);
    }
    // And the reverse: a tag the reporter honours but this file does
    // not know about would let a spec pass here and skip there.
    const inReporter = [
      ...reporter.matchAll(/^ {2}"(@[a-z-]+)":/gm),
    ].map((m) => m[1]);
    expect(inReporter.sort()).toEqual([...DECLARING_TAGS].sort());
  });
});
