/**
 * F135. `${pending.length} message(s) above were interrupted` was the
 * copy a user met after the backend crashed — the moment they most
 * need a sentence that reads like someone wrote it.
 *
 * The helper is tested here; the rule below is the part that matters,
 * because the next count rendered by hand fails before anyone sees it
 * on a screen. Same rule as the portal's, for the same habit in the
 * other client.
 */

import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { countOf, pluralise } from "./plural";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("countOf", () => {
  it("says message for one and messages for anything else", () => {
    expect(countOf(1, "message")).toBe("1 message");
    expect(countOf(0, "message")).toBe("0 messages");
    expect(countOf(7, "message")).toBe("7 messages");
  });

  it("takes an irregular plural, because English has them", () => {
    expect(countOf(1, "person", "people")).toBe("1 person");
    expect(countOf(2, "person", "people")).toBe("2 people");
  });

  it("pluralise gives the noun alone", () => {
    expect(pluralise(1, "run")).toBe("run");
    expect(pluralise(3, "run")).toBe("runs");
  });
});

function sourceFiles(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules") continue;
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) sourceFiles(path, found);
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) found.push(path);
  }
  return found;
}

/**
 * Every `word(s)` that sits inside a string literal.
 *
 * A character scanner rather than a regex over each line, and both
 * earlier attempts are the reason. Matching whole lines reported
 * `setStatus(s)`, `String(s)` and `test(s)` — six offenders, none of
 * them words. Restricting to quoted text with a per-line pattern then
 * missed the one defect this file exists for, because fixing it had
 * spread the template literal across two lines and a per-line
 * `` `[^`]*` `` needs both backticks on one.
 *
 * Comments are skipped: this helper's docstring quotes the defect, and
 * so does the note beside every fix.
 */
export function hedgesIn(source: string): Array<{ line: number; text: string }> {
  const out: Array<{ line: number; text: string }> = [];
  let line = 1;
  let i = 0;
  const push = (text: string, at: number) => out.push({ line: at, text });

  while (i < source.length) {
    const c = source[i];
    if (c === "\n") {
      line += 1;
      i += 1;
      continue;
    }
    if (c === "/" && source[i + 1] === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (c === "/" && source[i + 1] === "*") {
      i += 2;
      while (i < source.length && !(source[i] === "*" && source[i + 1] === "/")) {
        if (source[i] === "\n") line += 1;
        i += 1;
      }
      i += 2;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      const quote = c;
      const startLine = line;
      let body = "";
      i += 1;
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\\") {
          body += source[i] + (source[i + 1] ?? "");
          i += 2;
          continue;
        }
        if (source[i] === "\n") {
          // Only a template literal may span lines; an unterminated
          // quote is a syntax error the compiler will have caught.
          if (quote !== "`") break;
          line += 1;
        }
        body += source[i];
        i += 1;
      }
      i += 1;
      for (const m of body.matchAll(/([A-Za-z]{3,})\(s\)/g)) {
        if (/^https?$/i.test(m[1])) continue;
        push(m[0], startLine);
      }
      continue;
    }
    // Outside a string, and outside a comment: this is code, but JSX
    // text lives here too — `{n} card(s)` renders and is not in quotes,
    // which is how the third version of this rule still missed one.
    //
    // A call and a hedge are the same shape to a scanner: `setStatus(s)`
    // against `card(s)`. What separates them is what comes next. A call
    // is followed by `;`, `,`, `)` or `}`; prose carries on into a tag,
    // a newline or another word.
    // Start of a word, or `String(s)` matches as `tring(s)` — a
    // scanner that begins at every offset finds words inside words.
    const prev = i > 0 ? source[i - 1] : " ";
    const atWordStart = !/[A-Za-z0-9_$]/.test(prev);
    const ahead = source.slice(i);
    const m = atWordStart ? /^([a-z]{3,})\(s\)(.|\n|$)/.exec(ahead) : null;
    if (m && !";,)}".includes(m[2]) && !/^https?$/i.test(m[1])) {
      push(`${m[1]}(s)`, line);
      i += m[1].length + 3;
      continue;
    }
    i += 1;
  }
  return out;
}

describe("nothing hedges with \"(s)\"", () => {
  const files = sourceFiles(SRC);

  it("found the source to scan", () => {
    // A scan of nothing passes, which is worse than no scan.
    expect(files.length).toBeGreaterThan(30);
    expect(files.some((f) => f.endsWith("App.tsx"))).toBe(true);
  });

  it("no user-facing string says thing(s)", () => {
    const offenders: string[] = [];
    for (const file of files) {
      for (const hit of hedgesIn(readFileSync(file, "utf8"))) {
        offenders.push(`${file.slice(SRC.length + 1)}:${hit.line} ${hit.text}`);
      }
    }
    expect(
      offenders,
      [
        "Use countOf(n, 'message') so the singular case cannot come out",
        'as "1 message(s)" — F135.',
        "",
        ...offenders,
      ].join("\n"),
    ).toEqual([]);
  });
});
