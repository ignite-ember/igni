// @vitest-environment jsdom
/**
 * Component tests for ``MarkdownInline``.
 *
 * Two things make this more than a styling choice, and both are
 * pinned here: the preview sits inside a ``<button>`` that opens the
 * document, so nothing it renders may be interactive; and the row
 * clamps to two lines with ``-webkit-box``, which a single
 * block-level child silently breaks.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MarkdownInline, closeOpenMarks, dropEchoedTitle } from "./MarkdownInline";

afterEach(cleanup);

describe("repairing markdown cut mid-syntax", () => {
  it("closes an unterminated bold run", () => {
    // Previews arrive pre-truncated and the cut lands wherever it
    // lands. Left open, the marker renders literally.
    expect(closeOpenMarks("a **bold")).toBe("a **bold**");
  });

  it("closes an unterminated code span", () => {
    expect(closeOpenMarks("call `foo")).toBe("call `foo`");
  });

  it("closes an unterminated fence before anything else", () => {
    expect(closeOpenMarks("```py\nx = 1")).toBe("```py\nx = 1\n```");
  });

  it("reads ** as one marker, not two single stars", () => {
    const repaired = closeOpenMarks("**done**");
    expect(repaired).toBe("**done**");
  });

  it("leaves balanced text alone", () => {
    expect(closeOpenMarks("plain words")).toBe("plain words");
  });
});

describe("dropping a title the row already shows", () => {
  it("removes a heading that is exactly the name", () => {
    expect(dropEchoedTitle("# Session handling\n\nOverview", "Session handling")).toBe(
      "Overview",
    );
  });

  it("ignores case and leading blank lines", () => {
    expect(dropEchoedTitle("\n\n## AUTH MODEL\n\nBody", "Auth model")).toBe("Body");
  });

  it("keeps a heading that merely starts with the name", () => {
    // "Session handling in detail" is a different heading and says
    // something the row does not.
    const text = "# Session handling in detail\n\nBody";
    expect(dropEchoedTitle(text, "Session handling")).toBe(text);
  });

  it("drops a plain first line that is the name, heading or not", () => {
    // The complaint this exists to answer is seeing the name twice.
    // Whether the echo was marked up as a heading is not the point.
    expect(dropEchoedTitle("Session handling\n\nBody", "Session handling")).toBe("Body");
  });

  it("keeps a first line that merely opens with the name", () => {
    const text = "Session handling is a signed JWT flow.\n\nBody";
    expect(dropEchoedTitle(text, "Session handling")).toBe(text);
  });

  it("does nothing without a name", () => {
    expect(dropEchoedTitle("# Title\n\nBody", "")).toBe("# Title\n\nBody");
  });
});

describe("what reaches the DOM", () => {
  const blockChildren = (root: HTMLElement) =>
    Array.from(root.querySelectorAll("*")).filter((el) =>
      ["P", "UL", "OL", "LI", "PRE", "TABLE", "TR", "TD", "BLOCKQUOTE", "H1", "H2"].includes(
        el.tagName,
      ),
    );

  it("renders no block elements, which would break the two-line clamp", () => {
    const { container } = render(
      <MarkdownInline text={"## Heading\n\n- one\n- two\n\nTail."} />,
    );
    expect(blockChildren(container)).toHaveLength(0);
  });

  it("renders no links, which would be a click target inside a button", () => {
    const { container } = render(<MarkdownInline text="see [the docs](https://x.test)" />);
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(container.textContent).toContain("the docs");
  });

  it("drops images rather than loading one to show two lines of text", () => {
    const { container } = render(<MarkdownInline text="![alt](https://x.test/a.png) after" />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.textContent).toContain("after");
  });

  it("strips the syntax that made the preview look like source", () => {
    const { container } = render(
      <MarkdownInline text={"# Title\n\n**bold** and _italic_ and - a bullet"} />,
    );
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/\*\*/);
    expect(text).not.toMatch(/^#/m);
    expect(text).toContain("bold");
    expect(text).toContain("italic");
  });

  it("keeps a heading's emphasis without its size", () => {
    const { container } = render(<MarkdownInline text={"## Overview\n\nBody"} />);
    const heading = container.querySelector(".mdi-heading");
    expect(heading?.tagName).toBe("STRONG");
    expect(heading?.textContent).toBe("Overview");
  });

  it("keeps code recognisable as code", () => {
    const { container } = render(<MarkdownInline text={"run `igni login` first"} />);
    expect(container.querySelector("code.mdi-code")?.textContent).toBe("igni login");
  });

  it("renders a fenced block inline rather than as a block", () => {
    const { container } = render(<MarkdownInline text={"```sh\nigni login\n```"} />);
    expect(container.querySelectorAll("pre")).toHaveLength(0);
    expect(container.textContent).toContain("igni login");
  });
});

describe("length", () => {
  it("cuts back to a word boundary rather than mid-word", () => {
    const { container } = render(
      <MarkdownInline text="alpha bravo charlie delta echo foxtrot" limit={20} />,
    );
    const text = container.textContent ?? "";
    expect(text).toMatch(/…$/);
    expect(text).not.toMatch(/char…$/);
  });

  it("leaves a short preview unmarked", () => {
    const { container } = render(<MarkdownInline text="short" limit={100} />);
    expect(container.textContent).toBe("short");
  });

  it("renders nothing at all when there is nothing to show", () => {
    const { container } = render(<MarkdownInline text="   " />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the title was the whole preview", () => {
    // Dropping the echo can empty it. An empty box under the name
    // would read as a document with no content.
    const { container } = render(<MarkdownInline text="# Auth model" name="Auth model" />);
    expect(container.firstChild).toBeNull();
  });
});

describe("in a list row", () => {
  it("survives being placed inside a button", () => {
    // The real use: the whole row is one click target.
    render(
      <button type="button">
        <MarkdownInline text={"## Overview\n\n- one\n- two"} className="afs-row-snippet" />
      </button>,
    );
    const button = screen.getByRole("button");
    expect(button.querySelectorAll("a, button, input")).toHaveLength(0);
    expect(button.textContent).toContain("Overview");
  });
});
