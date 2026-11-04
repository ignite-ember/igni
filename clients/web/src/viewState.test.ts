// @vitest-environment node
/**
 * No view holds UI state outside the window's model.
 *
 * The structural half of `docs/HOST_OBJECT_MODEL.md`'s *Complete by
 * construction*: every `useState` and `useReducer` under `src/` is
 * found by parsing the source (the TypeScript compiler, not a regex),
 * and each must be listed in `viewState.allowlist.ts` under a category
 * that says why it is not the model's. Read that file's header for the
 * rule; this only enforces it.
 */

import fs from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import {
  BECAUSE,
  CATEGORIES,
  EXCLUDED,
  MODEL_NAMES,
  VIEW_STATE,
  type Category,
} from "./viewState.allowlist";

const SRC = path.resolve(__dirname);

interface Found {
  file: string;
  name: string;
  line: number;
}

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    const rel = path.relative(SRC, full).split(path.sep).join("/");
    if (entry.isDirectory()) {
      if (Object.keys(EXCLUDED).some((x) => `${rel}/` === x)) continue;
      out.push(...sources(full));
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

/** Every `useState` / `useReducer` call under `src/`. */
function viewStates(): Found[] {
  return sources(SRC).flatMap((full) =>
    statesIn(path.relative(SRC, full).split(path.sep).join("/"), fs.readFileSync(full, "utf8")),
  );
}

/** Every `useState` / `useReducer` call in one file, by the name of
 *  what it holds. */
function statesIn(file: string, text: string): Found[] {
  const found: Found[] = [];
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      const called = ts.isIdentifier(callee)
        ? callee.text
        : ts.isPropertyAccessExpression(callee)
          ? callee.name.text
          : "";
      if (called === "useState" || called === "useReducer") {
        let name = "(unnamed)";
        const holder = node.parent;
        if (ts.isVariableDeclaration(holder)) {
          if (ts.isArrayBindingPattern(holder.name)) {
            const first = holder.name.elements[0];
            if (first && ts.isBindingElement(first)) name = first.name.getText(source);
          } else {
            name = holder.name.getText(source);
          }
        }
        const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
        found.push({ file, name, line });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

function listed(): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const [file, byCategory] of Object.entries(VIEW_STATE)) {
    const names: string[] = [];
    for (const [category, list] of Object.entries(byCategory)) {
      expect(Object.keys(CATEGORIES)).toContain(category);
      names.push(...(list as string[]));
    }
    out.set(file, names.sort());
  }
  return out;
}

describe("no view holds UI state outside the window's model", () => {
  const found = viewStates();

  it("finds the views' state at all (the scan is not silently empty)", () => {
    expect(found.length).toBeGreaterThan(100);
    expect(found.some((f) => f.file === "App.tsx")).toBe(true);
  });

  it("every useState and useReducer is listed, with a reason, and nothing more is", () => {
    const actual = new Map<string, string[]>();
    for (const f of found) actual.set(f.file, [...(actual.get(f.file) ?? []), f.name]);
    const expected = listed();
    const problems: string[] = [];
    for (const [file, names] of actual) {
      const allowed = [...(expected.get(file) ?? [])];
      for (const name of names) {
        const at = allowed.indexOf(name);
        if (at >= 0) allowed.splice(at, 1);
        else {
          const where = found.find((f) => f.file === file && f.name === name);
          problems.push(
            `${file}:${where?.line} holds '${name}' in useState. If it is something the ` +
              "window shows, it belongs in the window model (clientState.ts); if not, list " +
              "it in viewState.allowlist.ts under the category that says why.",
          );
        }
      }
      for (const stale of allowed) {
        problems.push(`${file}: '${stale}' is listed but no view holds it — remove it`);
      }
    }
    for (const file of expected.keys()) {
      if (!actual.has(file)) problems.push(`${file} is listed but holds no state — remove it`);
    }
    expect(problems).toEqual([]);
  });

  it("nothing named like the model's is held outside it without saying why", () => {
    const problems: string[] = [];
    for (const f of found) {
      if (!MODEL_NAMES.test(f.name)) continue;
      if (BECAUSE[f.file]?.[f.name]) continue;
      problems.push(
        `${f.file}:${f.line} holds '${f.name}', which names something the window model ` +
          "describes (ui.*, sessions.current). Bind to the model instead — or, if it is " +
          "not that, say why in BECAUSE.",
      );
    }
    for (const [file, names] of Object.entries(BECAUSE)) {
      for (const name of Object.keys(names)) {
        if (!found.some((f) => f.file === file && f.name === name)) {
          problems.push(`BECAUSE explains ${file} '${name}', which no view holds`);
        }
      }
    }
    expect(problems).toEqual([]);
  });

  it("the shell holds none of what the model describes", () => {
    // The App is where window state used to live. Name by name: the
    // page stack, the panel, the sidebar, the preview, the selection,
    // the composer, toasts, the dialog, the scroll, focus and the
    // session are bound, not held.
    const shell = found.filter((f) => f.file === "App.tsx").map((f) => f.name);
    for (const field of [
      "pages",
      "panel",
      "sidebarOpen",
      "previewPath",
      "highlightedItemId",
      "composerSeed",
      "toasts",
      "showUpdateModal",
      "stickToBottom",
      "searchOpen",
      "sessionId",
      "projectDir",
      "pageHome",
    ]) {
      expect(shell).not.toContain(field);
    }
  });

  it("would catch a view keeping the model's state again", () => {
    // The rule, against a view written the old way.
    const regressed = statesIn(
      "App.tsx",
      `function App() {
         const [panel, setPanel] = useState(null);
         const [toasts, dispatch] = React.useReducer(reduce, []);
       }`,
    );
    expect(regressed.map((f) => f.name)).toEqual(["panel", "toasts"]);
    const unlisted = regressed.filter((f) => !(listed().get(f.file) ?? []).includes(f.name));
    expect(unlisted).toHaveLength(2);
    expect(regressed.every((f) => MODEL_NAMES.test(f.name) && !BECAUSE[f.file]?.[f.name])).toBe(
      true,
    );
  });

  it("every category has its reason", () => {
    for (const [category, reason] of Object.entries(CATEGORIES)) {
      expect(reason.length, category as Category).toBeGreaterThan(40);
    }
  });
});
