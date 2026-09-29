// @vitest-environment jsdom
/**
 * Typing to format, instead of hunting a toolbar.
 *
 * Two mechanisms, tested here because neither can be driven through
 * a synthetic DOM event: input rules run inside ProseMirror's own
 * text-input handling, which ``execCommand`` goes around.
 *
 * The filter is the part with a judgement in it. A menu you have to
 * read is a menu that has failed — the point of typing "/tab" is
 * that Enter lands on Table without looking.
 */
import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";

import { DOCUMENT_EXTENSIONS, markdownOf } from "./documentMarkdown";
import { SLASH_ITEMS, matching } from "./slashCommands";

/** Type into the editor the way a person does, through input rules. */
function type(editor: Editor, text: string) {
  const { view } = editor;
  for (const char of text) {
    const { from, to } = view.state.selection;
    const handled = view.someProp("handleTextInput", (rule) =>
      rule(view, from, to, char),
    );
    if (!handled) {
      view.dispatch(view.state.tr.insertText(char, from, to));
    }
  }
}

function editorWith(content = "") {
  return new Editor({ extensions: DOCUMENT_EXTENSIONS, content });
}

describe("markdown shortcuts while typing", () => {
  it("turns ## into a heading", () => {
    const editor = editorWith("<p></p>");

    type(editor, "## Overview");

    expect(markdownOf(editor).trim()).toBe("## Overview");
    editor.destroy();
  });

  it("turns - into a bulleted list", () => {
    const editor = editorWith("<p></p>");

    type(editor, "- first");

    expect(markdownOf(editor).trim()).toBe("- first");
    editor.destroy();
  });

  it("turns 1. into a numbered list", () => {
    const editor = editorWith("<p></p>");

    type(editor, "1. first");

    expect(markdownOf(editor).trim()).toBe("1. first");
    editor.destroy();
  });

  it("turns > into a quote", () => {
    const editor = editorWith("<p></p>");

    type(editor, "> careful");

    expect(markdownOf(editor).trim()).toBe("> careful");
    editor.destroy();
  });

  it("turns **text** into bold", () => {
    const editor = editorWith("<p></p>");

    type(editor, "say **this** loudly");

    expect(markdownOf(editor).trim()).toBe("say **this** loudly");
    editor.destroy();
  });

  it("turns `text` into code", () => {
    const editor = editorWith("<p></p>");

    type(editor, "read `SameSite` first");

    expect(markdownOf(editor).trim()).toBe("read `SameSite` first");
    editor.destroy();
  });

  it("leaves a hash in the middle of a sentence alone", () => {
    // "issue #14" is not a heading, and a rule that fired there
    // would rewrite somebody's sentence as they typed it.
    const editor = editorWith("<p></p>");

    type(editor, "see issue #14 for this");

    expect(markdownOf(editor).trim()).toBe("see issue #14 for this");
    editor.destroy();
  });
});

describe("what the slash menu offers", () => {
  it("offers everything before anything is typed", () => {
    expect(matching("")).toHaveLength(SLASH_ITEMS.length);
  });

  it("finds a block by the start of its name", () => {
    expect(matching("tab").map((i) => i.label)).toEqual(["Table"]);
  });

  it("finds one by a word somebody would reach for instead", () => {
    // Nobody types "/code block" looking for a YAML fence.
    expect(matching("yaml").map((i) => i.label)).toEqual(["Code block"]);
    expect(matching("steps").map((i) => i.label)).toEqual(["Numbered list"]);
  });

  it("matches the start of a word, not anywhere inside it", () => {
    // Substring matching would give "ist" every list in the menu,
    // and "ote" both Quote and Numbered list. A menu that needs
    // reading is the thing the typing was avoiding.
    expect(matching("ist")).toEqual([]);
    expect(matching("ote").map((i) => i.label)).toEqual([]);
  });

  it("does offer more than one when more than one starts that way", () => {
    // "co" is the start of both "code" and "columns", so it offers
    // Code block and Table. Two is an answer; the arrow keys are
    // right there.
    expect(matching("co").map((i) => i.label)).toEqual(["Code block", "Table"]);
  });

  it("is case-insensitive, because nobody holds shift for a menu", () => {
    expect(matching("QUOTE").map((i) => i.label)).toEqual(["Quote"]);
  });

  it("answers nothing for a typo rather than everything", () => {
    expect(matching("zzz")).toEqual([]);
  });
});

describe("what the slash items do", () => {
  it("every one of them inserts something", () => {
    for (const item of SLASH_ITEMS) {
      const editor = editorWith("<p>x</p>");
      const before = markdownOf(editor);
      const at = editor.state.selection;

      item.run(editor, { from: at.from, to: at.to });

      expect(markdownOf(editor), item.label).not.toBe(before);
      editor.destroy();
    }
  });

  it("the table it inserts is a real table, not text", () => {
    // The failure this whole area exists to prevent: a table that
    // serialises back as run-together cells.
    const editor = editorWith("<p></p>");
    const at = editor.state.selection;

    SLASH_ITEMS.find((i) => i.label === "Table")!.run(editor, {
      from: at.from,
      to: at.to,
    });

    expect(markdownOf(editor)).toContain("|");
    editor.destroy();
  });
});
