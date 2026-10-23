/**
 * Every class a published component names is defined in the
 * published stylesheet.
 *
 * An extension renders on its own origin under `default-src 'self'`.
 * It cannot link `theme.css`, and it is not supposed to: that file is
 * the app's, it changes without notice, and §8 excludes it from the
 * floor. So the only CSS a published component gets is whatever is in
 * `clients/ui/igni-ui.css`.
 *
 * Nothing used to check that. The result was seven components under
 * the compatibility floor — `OverflowMenu`, `Tooltip`, `Skeleton`,
 * `Monogram`, `MarkdownInline`, `MenuIcon` and the mermaid block —
 * naming thirty-two classes that only `theme.css` defined. Inside the
 * app they looked perfect, which is the whole problem: the one
 * environment they were promised for is the one nobody rendered them
 * in. `floor.test.ts` asserted they existed and were named. Existing
 * is not rendering.
 *
 * This reads the source rather than a rendered tree on purpose. A
 * render test only covers the branches it happens to take, and most
 * of these classes are on a hover state, a disabled state or an error
 * path. Reading `className=` catches every branch, including the ones
 * no test has an argument for yet.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { withoutComments } from "../../test-utils/sourceScan";

const here = dirname(fileURLToPath(import.meta.url));
const PUBLISHED_CSS = join(here, "..", "..", "..", "clients", "ui", "igni-ui.css");

/** Class names a stylesheet defines, comments stripped. */
function definedIn(path: string): Set<string> {
  const css = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  return new Set(
    [...css.matchAll(/\.(-?[A-Za-z_][\w-]*)/g)].map((m) => m[1]),
  );
}

/**
 * The source text of every `className=` value, with braces balanced.
 *
 * A regex that stops at the first `}` gets `className={a ? "x" : "y"}`
 * wrong, and that is not a corner case — it is how `MenuIcon` writes
 * its only class, which is exactly the one an earlier version of this
 * scan missed.
 */
function classExpressions(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/className\s*=\s*/g)) {
    const i = m.index + m[0].length;
    if (src[i] === '"') {
      out.push(src.slice(i, src.indexOf('"', i + 1) + 1));
      continue;
    }
    if (src[i] !== "{") continue;
    let depth = 0;
    for (let j = i; j < src.length; j++) {
      if (src[j] === "{") depth++;
      else if (src[j] === "}" && --depth === 0) {
        out.push(src.slice(i, j + 1));
        break;
      }
    }
  }
  return out;
}

/**
 * Class names inside one `className=` expression.
 *
 * `${...}` is blanked rather than guessed at, so a composed name like
 * `btn-${variant}` contributes nothing here. Those are covered by
 * `primitives.test.tsx`, which renders each variant and reads the
 * class off the DOM — the two tests split the work along the line
 * where reading source stops being able to tell.
 */
function namesIn(expr: string): string[] {
  // `a === "md"` names a variant, not a class. Comparisons go first,
  // or every union member a component switches on is reported.
  const scrubbed = expr.replace(/[!=]==?\s*(["'])[^"']*\1/g, "");

  const out: string[] = [];
  // A template's `${...}` holes are scanned in their own right before
  // being blanked. They are not decoration: `tip ${x ? "tip-below" :
  // "tip-above"}` keeps both of its class names in there, and a
  // scanner that only blanked the hole reported this file clean while
  // it applied two classes no stylesheet in the repository defined.
  const holes: string[] = [];
  const flat = scrubbed.replace(/\$\{([^{}]*)\}/g, (_, inner: string) => {
    holes.push(inner);
    return " ";
  });

  for (const m of flat.matchAll(/"([^"\n]*)"|'([^'\n]*)'|`([^`]*)`/g)) {
    for (const token of (m[1] ?? m[2] ?? m[3] ?? "").split(/\s+/)) {
      // A trailing hyphen is the stump of a blanked `${...}`, as in
      // `btn-${variant}`. Those are covered by COMPOSED below, which
      // can enumerate what source reading cannot.
      if (/^-?[A-Za-z_][\w-]*$/.test(token) && !token.endsWith("-")) {
        out.push(token);
      }
    }
  }
  for (const hole of holes) out.push(...namesIn(hole));
  return out;
}

const published = definedIn(PUBLISHED_CSS);
const sources = readdirSync(here)
  .filter((f) => /\.tsx?$/.test(f) && !f.includes(".test.") && f !== "index.ts")
  .sort();

/**
 * Every value in the published stylesheet, as a lookup.
 *
 * Both the `:root` fallbacks and the inline `var(--x, fallback)`
 * ones: an extension sees the inline fallback whenever its own page
 * has redefined `:root`, which a bundled framework stylesheet does
 * routinely.
 */
function fallbacksIn(path: string) {
  const css = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const root = /^:root\s*\{([\s\S]*?)^\}/m.exec(css);
  const declared = new Map<string, string>();
  for (const m of (root?.[1] ?? "").matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    declared.set(m[1], m[2].split(/\s+/).join(" ").trim());
  }
  const inline = new Map<string, string>();
  const after = css.slice(root?.index ?? 0);
  for (const m of after.matchAll(/var\((--[\w-]+),\s*([^()]*?)\)/g)) {
    inline.set(m[1], m[2].trim());
  }
  return { declared, inline };
}

describe("the published fallbacks are igni's values", () => {
  /*
   * The header of igni-ui.css promises "values match igni's dark
   * theme", and for eighteen of fifty they did not. Opened outside
   * igni — which is how an extension author develops, every day,
   * before they ever mount it — the radii were 6/10/14 against the
   * app's 8/12/18, every semantic colour was a different hue, and
   * the UI font was a different stack.
   *
   * That is the same mistake as resolving a token against an assumed
   * value instead of the declared one. It does not fail anything, it
   * does not look broken, and it means the thing an author tunes
   * their layout against is not the thing their users see.
   */
  const theme = fallbacksIn(join(here, "..", "..", "..", "clients", "web", "src", "theme.css")).declared;
  const published = fallbacksIn(PUBLISHED_CSS);

  it("declares nothing igni does not", () => {
    expect([...published.declared.keys()].filter((k) => !theme.has(k))).toEqual(
      [],
    );
  });

  it.each([...published.declared.entries()])(
    ":root %s matches theme.css",
    (name, value) => {
      expect(value).toBe(theme.get(name));
    },
  );

  /*
   * A variable a component owns rather than a token igni publishes.
   *
   * `Split` writes its width into one, per instance, so two splits
   * on a page do not fight over a single property. It has no theme
   * value and never will, so holding it to one would be holding it
   * to nothing.
   *
   * The convention earns its keep by being checkable: anything not
   * declared by `theme.css` must be named `--ui-…`, so a typo in a
   * real token name still fails here rather than being waved
   * through as component-local.
   */
  const isComponentLocal = (name: string) => name.startsWith("--ui-");

  it.each([...published.inline.entries()])(
    "var(%s) fallback matches theme.css",
    (name, value) => {
      if (!theme.has(name)) {
        expect(
          isComponentLocal(name),
          `${name} is not a token theme.css declares and is not named ` +
            `--ui-…, so it is either a typo or a component variable that ` +
            `needs renaming`,
        ).toBe(true);
        return;
      }
      expect(value).toBe(theme.get(name));
    },
  );
});

describe("the published stylesheet covers the published components", () => {
  it.each(sources)("%s names no class igni-ui.css lacks", (file) => {
    const src = withoutComments(readFileSync(join(here, file), "utf8"));
    const missing = new Set<string>();
    for (const expr of classExpressions(src)) {
      for (const name of namesIn(expr)) {
        if (!published.has(name)) missing.add(name);
      }
    }
    expect(
      [...missing].sort(),
      `${file} renders with classes clients/ui/igni-ui.css does not define, ` +
        `so an extension using it gets unstyled DOM. Port the rules from ` +
        `theme.css into igni-ui.css, with token fallbacks.`,
    ).toEqual([]);
  });

  /**
   * Names built from a prop, which `namesIn` blanks by design.
   *
   * This is the gap the README's first drift fell through:
   * `btn-warn` was a real variant the app styled and the published
   * stylesheet did not, and nothing noticed because no source line
   * contains the string "btn-warn" — it only ever exists as
   * `btn-${variant}` at runtime. Enumerating the unions here is the
   * only way a static check sees them.
   */
  const COMPOSED: Record<string, string[]> = {
    // ButtonVariant, less "default", which adds no class.
    "Button variant": ["btn-primary", "btn-danger", "btn-warn", "btn-quiet"],
    // ButtonSize, less "md".
    "Button size": ["btn-sm", "btn-xs"],
  };

  it.each(Object.entries(COMPOSED))(
    "%s: every value has a rule",
    (_label, names) => {
      expect(names.filter((n) => !published.has(n))).toEqual([]);
    },
  );

  it("finds classes at all", () => {
    // Guards the scanner itself: if `classExpressions` ever stopped
    // matching, every case above would pass by finding nothing.
    const src = withoutComments(
      readFileSync(join(here, "primitives.tsx"), "utf8"),
    );
    const found = classExpressions(src).flatMap(namesIn);
    expect(found).toContain("ui-card");
    expect(found.length).toBeGreaterThan(20);
  });
});
