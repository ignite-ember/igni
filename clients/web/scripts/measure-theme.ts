/**
 * The numbers `docs/DESIGN_SYSTEM.md` quotes, measured.
 *
 * Run: `npx tsx scripts/measure-theme.ts`
 *
 * Those numbers were produced once by a throwaway script and then
 * went stale, which is the predictable half. The unpredictable half
 * is that a stale number cannot be *corrected* honestly: re-measuring
 * with a slightly different definition — counting `gap` or not,
 * counting inside `@media` or not — gives a different answer that
 * looks like a correction and is really a second opinion. The first
 * attempt at fixing them produced 2146/1343 against a documented
 * 1789/1437 and neither was wrong; they were answers to different
 * questions.
 *
 * So the definition lives here, in code, and the document quotes this
 * script's output. When a number looks wrong, run it.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const THEME = join(here, "..", "src", "theme.css");
const PUBLISHED = join(here, "..", "..", "ui", "igni-ui.css");

/**
 * Properties that take a value from a scale.
 *
 * Shorthands are included and longhands with them: `padding` and
 * `padding-top` are both places a literal pixel can hide. `border`
 * is not here — its width is a hairline everywhere and never a scale
 * step — and neither is `width`/`height`, which are sizes rather
 * than rhythm.
 */
const SCALED = [
  "margin",
  "padding",
  "gap",
  "row-gap",
  "column-gap",
  "font-size",
  "line-height",
  "border-radius",
];

const PROP = new RegExp(
  `(?:^|[;{])\\s*(${SCALED.join("|")})(?:-[a-z]+)?\\s*:\\s*([^;{}]+)`,
  "g",
);

function withoutComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/**
 * Declarations that name a scale, against declarations that could.
 *
 * "Names a scale" means the value is made of `var(--…)` and keywords
 * — `0`, `auto`, `inherit`, `none`. A value mixing a token and a
 * literal (`var(--space-4) 3px`) counts as a literal, because the
 * literal is the part that drifts.
 */
function coverage(css: string) {
  let total = 0;
  let scaled = 0;
  for (const m of withoutComments(css).matchAll(PROP)) {
    const value = m[2].trim();
    if (!value) continue;
    total++;
    const bare = value.replace(/var\(--[\w-]+(?:,[^()]*)?\)/g, " ");
    if (/^[\s0]*$|^[\sa-z-]*$/.test(bare)) scaled++;
  }
  return { total, scaled };
}

function classesIn(css: string): Set<string> {
  return new Set(
    [...withoutComments(css).matchAll(/\.(-?[A-Za-z_][\w-]*)/g)].map(
      (m) => m[1],
    ),
  );
}

const theme = readFileSync(THEME, "utf8");
const published = readFileSync(PUBLISHED, "utf8");
const { total, scaled } = coverage(theme);

const rows: [string, string | number][] = [
  ["theme.css lines", theme.split("\n").length],
  ["theme.css classes", classesIn(theme).size],
  ["scalable declarations", total],
  ["…naming a scale", scaled],
  ["…coverage", `${((scaled / total) * 100).toFixed(1)}%`],
  ["igni-ui.css lines", published.split("\n").length],
  ["igni-ui.css classes", classesIn(published).size],
];

const width = Math.max(...rows.map(([label]) => label.length));
for (const [label, value] of rows) {
  console.log(`${label.padEnd(width)}  ${value}`);
}
