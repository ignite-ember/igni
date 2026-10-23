/**
 * Reading source as text, without reading its prose.
 *
 * The floor's two guards — `ui-react/styled.test.ts` and
 * `ui-react/boundary.test.ts` — work by scanning source for
 * `className=` and for import specifiers. Both found their first
 * false positive in a doc comment: `MenuIcon`'s docstring contains
 * the words `className="language-mermaid"` as an example, and
 * `Monogram`'s contains `as MA from "maya"` as an illustration of
 * how initials are derived. Neither is code, and a guard that cries
 * wolf about documentation gets switched off.
 *
 * This lives outside `ui-react/` deliberately: both guards scan every
 * non-test source file in their own directory, so a helper placed
 * beside them would be scanned as if it were part of the floor.
 */

/**
 * Source with its comments blanked out.
 *
 * Blanked rather than deleted, so byte offsets and line numbers still
 * line up with the original — a scanner that reports a position stays
 * truthful.
 *
 * String literals are tracked, so the `//` inside
 * `"https://example.test"` is not mistaken for a comment and the
 * `/*` inside a tsconfig path key like `"react/*"` does not open
 * one. That is not hypothetical on either count: the second broke
 * the test that reads `tsconfig.json`, which had been stripping
 * comments with a regex that could not tell the difference.
 *
 * JSON with comments parses correctly through this too, which is
 * what `tsconfig.json` is.
 */
export function withoutComments(src: string): string {
  let out = "";
  let i = 0;
  while (i < src.length) {
    const ch = src[i];

    // A string literal is copied through untouched. This is the
    // reason this is a scanner and not two regexes: the tsconfig key
    // `"react/*"` contains `/*`, and a stripper that does not know
    // it is inside a string treats it as a comment opener and eats
    // everything up to the next `*/` — which silently deleted half
    // of that file for the test that parses it.
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      out += ch;
      i++;
      while (i < src.length) {
        out += src[i];
        if (src[i] === "\\") {
          i++;
          if (i < src.length) out += src[i];
          i++;
          continue;
        }
        if (src[i] === quote) {
          i++;
          break;
        }
        i++;
      }
      continue;
    }

    if (ch === "/" && src[i + 1] === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end === -1 ? src.length : end + 2;
      // Newlines are kept so line numbers still line up with the
      // original — a scanner that reports a position stays truthful.
      for (const c of src.slice(i, stop)) out += c === "\n" ? "\n" : " ";
      i = stop;
      continue;
    }

    if (ch === "/" && src[i + 1] === "/") {
      while (i < src.length && src[i] !== "\n") {
        out += " ";
        i++;
      }
      continue;
    }

    out += ch;
    i++;
  }
  return out;
}
