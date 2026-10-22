// @vitest-environment jsdom
/**
 * A document goes into the rich-text editor and comes back out.
 *
 * Tested against the real store rather than a fixture, because both
 * failures this guards are ones a fixture would have missed. Tables
 * are in ten of seventy versions here and StarterKit has no idea
 * what one is — a table round-tripped without the extension comes
 * back as "SettingValueNotesMax sessions per user10", silently.
 *
 * And the wrapping. Saving a one-word change used to rewrite every
 * line of a hard-wrapped document, which in a product with a Compare
 * versions view means showing the whole document as changed — the
 * same as showing nothing.
 */
import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";

import {
  DOCUMENT_EXTENSIONS,
  blocksOf,
  markdownOf,
  preserveWrapping,
  sameContent,
} from "./documentMarkdown";

/**
 * A document lifted out of the running store, not invented here.
 *
 * Both failures this file guards were found in it and would have
 * been missed by anything written to demonstrate them: the table
 * because a fixture author reaches for paragraphs and lists, and the
 * wrapping because a fixture author writes one line per paragraph.
 */
import realDocumentText from "./__fixtures__/a-real-document.md?raw";

function throughEditor(md: string): string {
  const editor = new Editor({ extensions: DOCUMENT_EXTENSIONS, content: md });
  const out = markdownOf(editor);
  editor.destroy();
  return out;
}

const realDocument = () => realDocumentText;

describe("a document nobody edited", () => {
  it("comes back exactly as it went in", () => {
    // The whole point. An open-and-close must not be a change.
    const original = realDocument();

    const out = preserveWrapping(original, throughEditor(original));

    expect(out.trim()).toBe(original.trim());
  });

  it("keeps its table", () => {
    // Without the table extension this is where the cells run
    // together, and nothing raises.
    const original = realDocument();

    const out = preserveWrapping(original, throughEditor(original));

    expect(out).toContain("| Max sessions per user | 10 | Oldest evicted |");
  });

  it("keeps its fenced code byte for byte", () => {
    const original = realDocument();
    const fences = (s: string) => s.match(/```[\s\S]*?```/g) ?? [];

    const out = preserveWrapping(original, throughEditor(original));

    expect(fences(out)).toEqual(fences(original));
  });
});

describe("a document somebody edited", () => {
  it("rewraps only the paragraph that changed", () => {
    const original = "# Title\n\nOne line\nwrapped over two.\n\nAnother\nwrapped one.\n";
    const edited = "# Title\n\nOne line wrapped over two, now longer.\n\nAnother wrapped one.\n";

    const out = preserveWrapping(original, edited);

    // Touched: takes the new text.
    expect(out).toContain("One line wrapped over two, now longer.");
    // Untouched: keeps the line break it had.
    expect(out).toContain("Another\nwrapped one.");
  });

  it("does not let an edited paragraph borrow another's wrapping", () => {
    // Two paragraphs reading the same are still two paragraphs.
    const original = "Same\nwords.\n\nSame\nwords.\n";
    const edited = "Same words.\n\nSame words.\n";

    const out = preserveWrapping(original, edited);

    expect(out).toBe("Same\nwords.\n\nSame\nwords.\n");
  });

  it("keeps a block that only moved", () => {
    const original = "# T\n\nFirst\npara.\n\nSecond\npara.\n";
    const edited = "# T\n\nSecond para.\n\nFirst para.\n";

    const out = preserveWrapping(original, edited);

    expect(out).toBe("# T\n\nSecond\npara.\n\nFirst\npara.\n");
  });

  it("takes a genuinely new paragraph as the editor wrote it", () => {
    const original = "# T\n\nOne.\n";
    const edited = "# T\n\nOne.\n\nA brand new paragraph.\n";

    const out = preserveWrapping(original, edited);

    expect(out).toContain("A brand new paragraph.");
  });

  it("keeps the file's trailing newline, or its absence", () => {
    expect(preserveWrapping("A\n", "A\n")).toBe("A\n");
    expect(preserveWrapping("A", "A\n")).toBe("A");
  });

  it("gives a CRLF document back with CRLF throughout", () => {
    // Found on the Windows runner, where git checks the fixture out
    // with CRLF. The blocks were the original's bytes and the joins
    // between them were written in this module, so the result was
    // CRLF inside paragraphs and a bare LF between them. Mixed
    // endings read as a change on every paragraph boundary in the
    // Compare versions view — the failure this file exists for,
    // arriving on one platform only.
    const original = "# Title\r\n\r\nOne line\r\nwrapped over two.\r\n";
    const edited = "# Title\n\nOne line\nwrapped over two.\n";

    const out = preserveWrapping(original, edited);

    expect(out).toBe(original);
    expect(out).not.toMatch(/[^\r]\n/);
  });
});

describe("splitting a document into blocks", () => {
  it("keeps a fenced block whole, blank lines and all", () => {
    const md = "Before\n\n```yaml\na: 1\n\nb: 2\n```\n\nAfter";

    expect(blocksOf(md)).toEqual(["Before", "```yaml\na: 1\n\nb: 2\n```", "After"]);
  });

  it("does not let a ``` inside a ~~~ block close it", () => {
    const md = "~~~\nshowing ```\nstill inside\n~~~\n\nAfter";

    expect(blocksOf(md)).toHaveLength(2);
  });
});

describe("deciding whether a block is the same block", () => {
  it("ignores how a paragraph was wrapped", () => {
    expect(sameContent("one two\nthree", "one two three")).toBe(true);
  });

  it("ignores the marker a blockquote repeats on every line", () => {
    expect(sameContent("> one\n> two", "> one two")).toBe(true);
  });

  it("does not ignore whitespace inside code", () => {
    // Indentation is content in a fence, and a "same block" that
    // reflowed somebody's YAML would be a wrong answer.
    expect(sameContent("```\n  a: 1\n```", "```\na: 1\n```")).toBe(false);
  });

  it("notices an actual change", () => {
    expect(sameContent("one two", "one three")).toBe(false);
  });
});
