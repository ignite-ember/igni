/**
 * The stylesheet is syntactically whole.
 *
 * ``theme.css`` ended mid-rule: 1161 ``{`` against 1160 ``}``, with
 * ``.demo-page-composer-send:disabled`` opened on the last two lines and
 * never closed. esbuild recovered and only warned —
 *
 *     ▲ [WARNING] Expected "}" to go with "{" [css-syntax-error]
 *       The unbalanced "{" is here: .demo-page-composer-send:disabled {
 *
 * — so every build passed and the warning scrolled by unread.
 *
 * The reason it matters is not the warning. It is that the next rule
 * appended to this file would land *inside* that unclosed block and
 * silently never apply, and whoever added it would have no reason to
 * suspect the file rather than their selector.
 *
 * Counting braces is crude and does not know about strings or comments. It
 * is enough for the failure that actually happened — a truncated file —
 * and it costs nothing.
 */

import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));

const stylesheets = readdirSync(HERE).filter((f) => f.endsWith(".css"));

describe("stylesheets are not truncated", () => {
  it("finds at least one to check", () => {
    // A rule that silently checks nothing is worse than no rule.
    expect(stylesheets.length).toBeGreaterThan(0);
  });

  it.each(stylesheets)("%s has balanced braces", (name) => {
    const text = readFileSync(path.join(HERE, name), "utf8");
    const open = (text.match(/\{/g) ?? []).length;
    const close = (text.match(/\}/g) ?? []).length;

    expect(
      { file: name, open, close },
      `${name} has ${open} "{" and ${close} "}". A file that ends mid-rule ` +
        `still builds — esbuild recovers with a warning — but the next rule ` +
        `appended to it lands inside the unclosed block and never applies.`,
    ).toEqual({ file: name, open, close: open });
  });

  it.each(stylesheets)("%s does not end inside a rule", (name) => {
    const text = readFileSync(path.join(HERE, name), "utf8").trimEnd();

    // The exact shape of the bug: the last meaningful character was a
    // declaration's semicolon rather than a closing brace.
    expect(text.endsWith("}") || text.endsWith("*/")).toBe(true);
  });
});
